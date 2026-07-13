"""HyDE query transformation — is a hypothetical document better than translation?

HyDE (Gao et al. 2022) replaces the query embedding with the embedding of an
LLM-generated *hypothetical document* that answers it — here, a hypothetical
USDA-style food entry. We test whether that beats the pipeline's plain translation:

  - translated       : the English name (current production transform)
  - HyDE-from-raw     : LLM writes a hypothetical entry from the Hungarian/German name
  - HyDE-from-transl.  : LLM expands the English translation into a hypothetical entry

Same corpus (USDA), same embedder (bge-m3), hard gold. Per-stratum Success@1 with
bootstrap CIs and McNemar vs translation. Generated docs are cached (gitignored) so
the LLM is called once. Aggregate metrics only.

Usage:
    SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
      uv run python scripts/eval_food_hyde.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np

from syncology import db
from syncology.resolve import embedders, food_eval, food_labels, foods

K = 10
STRATA = ("simple", "compound", "prepared", "branded", "regional")


def _cached_hyde(tag: str, keys: list[str], texts: list[str]) -> list[str]:
    cache = Path(f"data/clean/hyde_{tag}.json")
    have = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [k for k in keys if k not in have]
    if todo:
        gen = foods.generate_hypothetical_docs([texts[keys.index(k)] for k in todo])
        have.update(dict(zip(todo, gen)))
        cache.write_text(json.dumps(have, ensure_ascii=False, indent=1))
    return [have[k] for k in keys]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()

    gold = food_labels.load_gold()
    con = duckdb.connect(args.db, read_only=True)
    descs = [r[0] for r in con.execute("SELECT description FROM foods").fetchall()]
    g = con.execute(
        f"""SELECT y.product, m.en_name FROM yazio_foods y JOIN food_map m USING(product)
            WHERE y.product IN ({','.join('?' for _ in gold)})""",
        list(gold),
    ).fetchall()
    con.close()
    products = [x[0] for x in g]
    en_names = [x[1] or x[0] for x in g]
    kws = [gold[p] for p in products]
    strata = np.array([food_labels.classify(p, e) for p, e in zip(products, en_names)])

    hyde_raw = _cached_hyde("raw", products, products)
    hyde_tr = _cached_hyde("translated", products, en_names)

    corpus_stems = [food_labels._stems(d) for d in descs]
    n_rel = [sum(1 for ds in corpus_stems if any(ks <= ds for ks in
             [food_labels._stems(k) for k in kw])) for kw in kws]

    emb = embedders.Embedder(embedders.SPECS["bge-m3"], batch_size=128)
    doc = np.load("data/clean/bench_docemb_bge-m3.npy").astype(np.float32)

    def evaluate(texts):
        qv = emb.encode_queries(texts).astype(np.float32)
        topk = np.argsort(-(qv @ doc.T), axis=1)[:, :K]
        outs, s1 = [], []
        for i in range(len(products)):
            hits = [j for j, d in enumerate(topk[i]) if food_labels.is_correct(descs[d], kws[i])]
            outs.append(food_eval.QueryOutcome(hit_ranks=hits, n_relevant=n_rel[i]))
            s1.append(bool(hits and hits[0] == 0))
        return food_eval.aggregate(outs), np.array(s1)

    print(f"gold N={len(products)}  strata={ {s:int((strata==s).sum()) for s in STRATA} }")
    print("\n" + "=" * 82)
    print(f"{'system':<20}{'Success@1 [95% CI]':>24}{'S@5':>8}{'MRR':>8}{'nDCG@10':>9}")
    print("-" * 82)
    s1 = {}
    for name, texts in (("translated", en_names), ("HyDE-from-raw", hyde_raw),
                        ("HyDE-from-transl", hyde_tr)):
        arr, s1[name] = evaluate(texts)
        m, lo, hi = food_eval.bootstrap_ci(arr["success@1"])
        print(f"{name:<20}{f'{m:.3f} [{lo:.3f},{hi:.3f}]':>24}{arr['success@5'].mean():>8.3f}"
              f"{arr['mrr'].mean():>8.3f}{arr['ndcg@10'].mean():>9.3f}")

    print("\nMcNemar vs translated:")
    for name in ("HyDE-from-raw", "HyDE-from-transl"):
        r = food_eval.mcnemar_exact(s1[name], s1["translated"])
        print(f"  {name:<18} +{r['a_only']} / -{r['b_only']}  p={r['p_value']:.3g}")

    print("\nPER-STRATUM Success@1  translated → HyDE-from-transl:")
    for s in STRATA:
        idx = strata == s
        if idx.any():
            print(f"  {s:<10} N={int(idx.sum()):<3} {s1['translated'][idx].mean():.3f} "
                  f"→ {s1['HyDE-from-transl'][idx].mean():.3f}")


if __name__ == "__main__":
    main()
