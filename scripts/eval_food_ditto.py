"""Ditto-style attribute matching — does serializing macros help the branded tail?

Ditto (Li et al. 2020) serializes a record's structured attributes into the text a
pretrained LM sees, so the model can attend to attribute values jointly with the
name. Both datasets carry per-100g macros, so we serialize them into query *and*
corpus — `"oat milk (40 kcal, 1.1 g protein, 1.5 g fat, 6.7 g carbs)"` — and retrieve
with the same embedder (bge-m3). The hypothesis: on branded products, where the
translated name collapses to an ambiguous generic, macros disambiguate the concept.

Compares name-only vs attribute-serialized retrieval on the enlarged, strata-
adjudicated gold (branded N≈27), per stratum, with bootstrap CIs and McNemar. Uses
the USDA corpus (branded concepts resolve to USDA generics). Translated query.

Usage:
    SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_strata.json \
      uv run python scripts/eval_food_ditto.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np

from syncology import db
from syncology.resolve import embedders, food_eval, food_labels

K = 10
STRATA = ("simple", "compound", "prepared", "branded", "regional")


def _serialize(name: str, e, p, f, c) -> str:
    def g(x):
        return f"{float(x):.0f}" if x is not None else "?"
    return (f"{name} ({g(e)} kcal, {g(p)} g protein, {g(f)} g fat, {g(c)} g carbs "
            "per 100g)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()

    meta = food_labels.load_gold_meta()
    con = duckdb.connect(args.db, read_only=True)
    corpus = con.execute(
        "SELECT description, energy_kcal, protein_g, fat_g, carbs_g FROM foods"
    ).fetchall()
    descs = [r[0] for r in corpus]
    q = con.execute(
        f"""SELECT y.product, m.en_name, y.energy_kcal, y.protein_g, y.fat_g, y.carbs_g
            FROM yazio_foods y JOIN food_map m USING(product)
            WHERE y.product IN ({','.join('?' for _ in meta)})""",
        list(meta),
    ).fetchall()
    con.close()
    products = [r[0] for r in q]
    en_names = [r[1] or r[0] for r in q]
    kws = [meta[r[0]][0] for r in q]
    strata = np.array([food_labels.stratum_of(r[0], r[1], meta[r[0]][1]) for r in q])
    q_macros = [(r[2], r[3], r[4], r[5]) for r in q]

    corpus_stems = [food_labels._stems(d) for d in descs]
    n_rel = [sum(1 for ds in corpus_stems if any(ks <= ds for ks in
             [food_labels._stems(k) for k in kw])) for kw in kws]

    emb = embedders.Embedder(embedders.SPECS["bge-m3"], batch_size=128)
    doc_name = np.load("data/clean/bench_docemb_bge-m3.npy").astype(np.float32)
    ditto_cache = Path("data/clean/ditto_docemb_bge-m3.npy")
    if ditto_cache.exists() and len(np.load(ditto_cache)) == len(descs):
        doc_ditto = np.load(ditto_cache).astype(np.float32)
    else:
        doc_ditto = emb.encode_docs([_serialize(*r) for r in corpus]).astype(np.float32)
        np.save(ditto_cache, doc_ditto)

    q_name = emb.encode_queries(en_names).astype(np.float32)
    q_ditto = emb.encode_queries(
        [_serialize(n, *m) for n, m in zip(en_names, q_macros)]).astype(np.float32)

    def evaluate(qv, docv):
        topk = np.argsort(-(qv @ docv.T), axis=1)[:, :K]
        outs, s1 = [], []
        for i in range(len(products)):
            hits = [j for j, d in enumerate(topk[i]) if food_labels.is_correct(descs[d], kws[i])]
            outs.append(food_eval.QueryOutcome(hit_ranks=hits, n_relevant=n_rel[i]))
            s1.append(bool(hits and hits[0] == 0))
        return food_eval.aggregate(outs), np.array(s1)

    print(f"enlarged gold N={len(products)}  strata={ {s:int((strata==s).sum()) for s in STRATA} }")
    print("\n" + "=" * 84)
    print(f"{'system':<14}{'Success@1 [95% CI]':>24}{'S@5':>8}{'MRR':>8}{'nDCG@10':>9}")
    print("-" * 84)
    results = {}
    for name, qv, docv in (("name-only", q_name, doc_name), ("ditto (+macros)", q_ditto, doc_ditto)):
        arr, s1 = evaluate(qv, docv)
        results[name] = s1
        m, lo, hi = food_eval.bootstrap_ci(arr["success@1"])
        print(f"{name:<14}{f'{m:.3f} [{lo:.3f},{hi:.3f}]':>24}{arr['success@5'].mean():>8.3f}"
              f"{arr['mrr'].mean():>8.3f}{arr['ndcg@10'].mean():>9.3f}")

    r = food_eval.mcnemar_exact(results["ditto (+macros)"], results["name-only"])
    print(f"\nMcNemar ditto vs name-only: +{r['a_only']} / -{r['b_only']}  p={r['p_value']:.3g}")
    print("\nPER-STRATUM Success@1  name-only → ditto:")
    for s in STRATA:
        idx = strata == s
        if idx.any():
            print(f"  {s:<10} N={int(idx.sum()):<3} {results['name-only'][idx].mean():.3f} "
                  f"→ {results['ditto (+macros)'][idx].mean():.3f}")


if __name__ == "__main__":
    main()
