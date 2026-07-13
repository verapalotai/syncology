"""Confidence-routed cascade: USDA-first, OFF-fallback below a cosine threshold.

The corpus ablation showed naive USDA+OFF concatenation recovers the OOV tail but
dilutes the clean head (a wash overall). Note that concatenation is exactly a
*max-confidence* router — argmax over the union picks whichever corpus has the higher
top-1 cosine — so OFF's spuriously-high exact string matches (`apple`→OFF "apple")
displace correct USDA generics. A **threshold cascade** routes on USDA confidence
*alone*: trust USDA's top-1 whenever its cosine ≥ τ, fall back to OFF only when USDA
itself is unconfident. The hypothesis: the confident head and the OOV tail are
cosine-separable, so a broad τ window recovers the tail at no head cost.

We report the whole accuracy-vs-τ frontier (no single tuned τ), the three baselines
(USDA-only, OFF-only, max-confidence merge), and a bootstrap-CI comparison at a
label-free τ (the median USDA top-1 cosine).

Usage:
    SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
      uv run python scripts/eval_food_cascade.py
"""

from __future__ import annotations

import argparse

import duckdb
import numpy as np

from syncology import db
from syncology.resolve import embedders, food_eval, food_labels

STRATA = ("simple", "compound", "prepared", "branded", "regional")
HEAD = ("simple", "compound", "prepared")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument("--query", default="translated", choices=["translated", "raw"])
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
    texts = en_names if args.query == "translated" else products
    kws = [gold[p] for p in products]
    strata = np.array([food_labels.classify(p, e) for p, e in zip(products, en_names)])

    usda_emb = np.load("data/clean/bench_docemb_bge-m3.npy").astype(np.float32)
    off_emb = np.load("data/clean/off_docemb_bge-m3.npy").astype(np.float32)
    q = embedders.Embedder(embedders.SPECS["bge-m3"]).encode_queries(texts).astype(np.float32)

    usda_sims = q @ usda_emb.T
    off_sims = q @ off_emb.T
    u_idx, o_idx = usda_sims.argmax(1), off_sims.argmax(1)
    u_cos = usda_sims[np.arange(len(q)), u_idx]
    o_cos = off_sims[np.arange(len(q)), o_idx]
    u_ok = np.array([food_labels.is_correct(usda_descs[u_idx[i]], kws[i]) for i in range(len(q))])
    o_ok = np.array([food_labels.is_correct(off_descs[o_idx[i]], kws[i]) for i in range(len(q))])

    def acc(mask, sub=None):
        m = mask if sub is None else mask[strata == sub] if (strata == sub).any() else np.array([])
        return m.mean() if len(m) else float("nan")

    def cascade(tau):
        return food_eval.cascade_route(u_cos, u_ok, o_ok, tau)

    print(f"query={args.query}  N={len(q)}  strata={ {s:int((strata==s).sum()) for s in STRATA} }")
    print(f"USDA top-1 cosine: min={u_cos.min():.3f} median={np.median(u_cos):.3f} max={u_cos.max():.3f}")
    print("\nBASELINES (Success@1):")
    merge_ok = np.where(u_cos >= o_cos, u_ok, o_ok)  # == combined-argmax / max-confidence
    print(f"  USDA-only          {acc(u_ok):.3f}")
    print(f"  OFF-only           {acc(o_ok):.3f}")
    print(f"  max-conf merge     {acc(merge_ok):.3f}   (= naive concatenation)")

    print("\nCASCADE FRONTIER (route to OFF when USDA cosine < τ):")
    print(f"  {'τ':>5} {'%→OFF':>6} {'overall':>8} {'head':>7} {'regional':>9} {'branded':>8} {'simple':>7}")
    for tau in np.round(np.arange(0.40, 0.86, 0.05), 2):
        c = cascade(tau)
        routed = float((u_cos < tau).mean())
        head = c[np.isin(strata, HEAD)].mean()
        print(f"  {tau:>5.2f} {routed:>6.0%} {c.mean():>8.3f} {head:>7.3f} "
              f"{acc(c,'regional'):>9.3f} {acc(c,'branded'):>8.3f} {acc(c,'simple'):>7.3f}")

    # label-free operating point: route only the least-confident decile to OFF
    tau_star = float(np.quantile(u_cos, 0.10))
    c = cascade(tau_star)
    print(f"\nOPERATING POINT  τ* = P10(USDA cosine) = {tau_star:.3f}  ({(u_cos<tau_star).mean():.0%} → OFF)")
    for name, arr in (("USDA-only", u_ok), ("max-conf merge", merge_ok), (f"cascade@{tau_star:.2f}", c)):
        m, lo, hi = food_eval.bootstrap_ci(arr.astype(float))
        print(f"  {name:<18} {m:.3f} [{lo:.3f}, {hi:.3f}]")
    for label, a, b in (("cascade vs USDA-only", c, u_ok), ("cascade vs merge", c, merge_ok)):
        r = food_eval.mcnemar_exact(a, b)
        print(f"  McNemar {label:<22} +{r['a_only']} / -{r['b_only']}  p={r['p_value']:.3g}")

    print("\nPER-STRATUM: USDA-only → cascade@τ*  (does the tail recover at no head cost?)")
    for s in STRATA:
        print(f"  {s:<10} N={int((strata==s).sum()):<3} {acc(u_ok,s):.3f} → {acc(c,s):.3f}")


if __name__ == "__main__":
    main()
