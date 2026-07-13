"""Corpus ablation: does adding Open Food Facts recover the coverage tail?

The stratified benchmark localizes the residual error to `regional`/OOV foods (absent
from USDA) and `branded` products. The thesis: this is a *coverage* gap, closed by a
second corpus — not a modelling gap. Here we test it directly. For the fixed
production embedder (bge-m3) we retrieve the hard gold against two corpora — USDA
only vs USDA + OFF — for both raw and translated queries, and report per-stratum
Success@1 with a McNemar test on the corpus effect. We also show *where* the winning
match came from (USDA vs OFF), so a recovered `lecsó` is visibly an OFF row.

Adding 360k branded/regional distractors could also *hurt* the head (simple/prepared)
— that trade-off is exactly what the per-stratum split surfaces.

Usage:
    SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
      uv run python scripts/eval_food_corpus.py
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


def _load_off_embeddings(descs: list[str]) -> np.ndarray:
    cache = Path("data/clean/off_docemb_bge-m3.npy")
    if cache.exists():
        emb = np.load(cache)
        if len(emb) == len(descs):
            return emb
    emb = embedders.Embedder(embedders.SPECS["bge-m3"], batch_size=128).encode_docs(descs)
    np.save(cache, emb)
    return emb


def _success1(q, corpus_emb, descs, kws):
    """Per-query Success@1 (bool) and the top-1 doc index, for one corpus."""
    top1 = np.argmax(q @ corpus_emb.T, axis=1)
    s1 = np.array([food_labels.is_correct(descs[top1[i]], kws[i]) for i in range(len(kws))])
    return s1, top1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()

    gold = food_labels.load_gold()
    con = duckdb.connect(args.db, read_only=True)
    usda_descs = [r[0] for r in con.execute("SELECT description FROM foods").fetchall()]
    off_descs = [r[0] for r in con.execute("SELECT description FROM off_foods").fetchall()]
    g = con.execute(
        f"""SELECT y.product, m.en_name FROM yazio_foods y JOIN food_map m USING(product)
            WHERE y.product IN ({','.join('?' for _ in gold)})""",
        list(gold),
    ).fetchall()
    con.close()
    products = [x[0] for x in g]
    en_names = [x[1] or x[0] for x in g]
    kws = [gold[p] for p in products]
    strata = [food_labels.classify(p, e) for p, e in zip(products, en_names)]

    usda_emb = np.load("data/clean/bench_docemb_bge-m3.npy").astype(np.float32)
    off_emb = _load_off_embeddings(off_descs).astype(np.float32)
    comb_descs = usda_descs + off_descs
    comb_emb = np.vstack([usda_emb, off_emb])
    n_usda = len(usda_descs)
    print(f"corpora: USDA={n_usda:,}  OFF={len(off_descs):,}  combined={len(comb_descs):,}")
    print(f"gold N={len(products)}  strata={ {s: strata.count(s) for s in STRATA} }")

    emb = embedders.Embedder(embedders.SPECS["bge-m3"])
    for qlabel, texts in (("raw", products), ("translated", en_names)):
        q = emb.encode_queries(texts).astype(np.float32)
        s1_usda, _ = _success1(q, usda_emb, usda_descs, kws)
        s1_comb, top1_comb = _success1(q, comb_emb, comb_descs, kws)

        print("\n" + "=" * 84)
        print(f"QUERY = {qlabel}   Success@1 per stratum:  USDA  ->  USDA+OFF")
        print("=" * 84)
        for cls in STRATA:
            idx = [i for i, c in enumerate(strata) if c == cls]
            if not idx:
                continue
            u = s1_usda[idx].mean()
            c = s1_comb[idx].mean()
            # how many combined-corpus wins came from OFF rows
            off_wins = sum(1 for i in idx if s1_comb[i] and top1_comb[i] >= n_usda)
            arrow = "→" if abs(c - u) < 1e-9 else ("↑" if c > u else "↓")
            print(f"  {cls:<10} N={len(idx):<3} {u:.3f} {arrow} {c:.3f}"
                  f"   (OFF supplied {off_wins} of {int(s1_comb[idx].sum())} hits)")
        mc = food_eval.mcnemar_exact(s1_comb, s1_usda)
        print(f"  OVERALL     N={len(products)} {s1_usda.mean():.3f} → {s1_comb.mean():.3f}"
              f"   McNemar: +OFF fixed {mc['a_only']}, broke {mc['b_only']}, p={mc['p_value']:.3g}")

        # qualitative: regional recovery
        reg = [i for i, c in enumerate(strata) if c == "regional"]
        if reg:
            print(f"  regional detail ({qlabel}):")
            for i in reg:
                src = "OFF" if top1_comb[i] >= n_usda else "USDA"
                mark = "✓" if s1_comb[i] else "✗"
                print(f"    {mark} {en_names[i]:<28} → [{src}] {comb_descs[top1_comb[i]][:48]!r}")


if __name__ == "__main__":
    main()
