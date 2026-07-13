"""Rigorous evaluation of the food-retrieval systems.

Adds to the headline benchmark: bootstrap 95% CIs on every metric, a per-difficulty
-stratum breakdown, and McNemar exact tests for the load-bearing claims
(translation helps; the cheap embedder isn't beaten by the 4B; reranking doesn't
help). Reuses the cached corpus embeddings. Public metrics only.

Usage:
    uv run python scripts/eval_food_rigor.py [--models a,b,c]
"""

from __future__ import annotations

import argparse
from collections import Counter

import duckdb
import numpy as np

from syncology import db
from syncology.resolve import embedders, food_eval, food_labels

K = 10


def _outcomes(sims_row_topk_idx, descs, kws, n_rel):
    hits = [j for j, idx in enumerate(sims_row_topk_idx) if food_labels.is_correct(descs[idx], kws)]
    return food_eval.QueryOutcome(hit_ranks=hits, n_relevant=n_rel)


def _fmt(mean, lo, hi):
    return f"{mean:.3f} [{lo:.3f},{hi:.3f}]"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="bge-m3,qwen3-0.6b,qwen3-4b,harrier-0.6b")
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()

    gold = food_labels.load_gold()
    con = duckdb.connect(args.db, read_only=True)
    corpus = con.execute("SELECT description FROM foods").fetchall()
    descs = [r[0] for r in corpus]
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
    print(f"gold N={len(products)} | strata: {dict(Counter(strata))}")

    # n_relevant per query (corpus stems precomputed once) — only affects nDCG's ideal.
    corpus_stems = [food_labels._stems(d) for d in descs]
    n_rel = []
    for kw in kws:
        kss = [food_labels._stems(k) for k in kw]
        n_rel.append(sum(1 for ds in corpus_stems if any(ks <= ds for ks in kss)))

    print("\n" + "=" * 92)
    print(f"{'model':<13}{'query':>11}{'Success@1 [95% CI]':>24}{'Success@5':>13}"
          f"{'MRR':>13}{'nDCG@10':>13}")
    print("=" * 92)
    s1_arrays: dict[str, np.ndarray] = {}  # for McNemar
    for name in args.models.split(","):
        name = name.strip()
        try:
            emb = embedders.Embedder(embedders.SPECS[name])
        except Exception as e:  # noqa: BLE001
            print(f"{name}: LOAD FAILED {type(e).__name__}")
            continue
        doc = np.load(f"data/clean/bench_docemb_{name}.npy")
        for qlabel, texts in (("raw", products), ("translated", en_names)):
            q = emb.encode_queries(texts)
            topk = np.argsort(-(q @ doc.T), axis=1)[:, :K]
            outs = [_outcomes(topk[i], descs, kws[i], n_rel[i]) for i in range(len(products))]
            arr = food_eval.aggregate(outs)
            s1 = food_eval.bootstrap_ci(arr["success@1"])
            s5 = arr["success@5"].mean()
            mrr = arr["mrr"].mean()
            ndcg = arr["ndcg@10"].mean()
            s1_arrays[f"{name}/{qlabel}"] = arr["success@1"]
            print(f"{name:<13}{qlabel:>11}{_fmt(*s1):>24}{s5:>13.3f}{mrr:>13.3f}{ndcg:>13.3f}")
        del emb

    # --- per-stratum breakdown for the production system (bge-m3 translated) ---
    prod = "bge-m3"
    emb = embedders.Embedder(embedders.SPECS[prod])
    doc = np.load(f"data/clean/bench_docemb_{prod}.npy")
    q = emb.encode_queries(en_names)
    topk20 = np.argsort(-(q @ doc.T), axis=1)[:, :20]
    outs = [_outcomes(topk20[i][:K], descs, kws[i], n_rel[i]) for i in range(len(products))]
    s1_prod = np.array([food_eval.query_metrics(o)["success@1"] for o in outs])
    print("\n" + "-" * 92)
    print(f"PER-STRATUM Success@1  ({prod}, translated)")
    for cls in ("simple", "compound", "prepared", "branded", "regional"):
        idx = [i for i, c in enumerate(strata) if c == cls]
        if not idx:
            continue
        m, lo, hi = food_eval.bootstrap_ci(s1_prod[idx])
        print(f"  {cls:<10} N={len(idx):<3} {_fmt(m, lo, hi)}")

    # --- McNemar significance tests ---
    print("\n" + "-" * 92)
    print("McNEMAR EXACT TESTS (paired Success@1)")

    def test(label, a_key, b_key, a=None, b=None):
        a = s1_arrays[a_key] if a is None else a
        b = s1_arrays[b_key] if b is None else b
        r = food_eval.mcnemar_exact(a, b)
        sig = "***" if r["p_value"] < 0.001 else "**" if r["p_value"] < 0.01 else \
              "*" if r["p_value"] < 0.05 else "ns"
        print(f"  {label:<44} a_only={r['a_only']:<3} b_only={r['b_only']:<3} "
              f"p={r['p_value']:.4g} {sig}")

    test("translation vs raw (bge-m3)", "bge-m3/translated", "bge-m3/raw")
    if "qwen3-4b/translated" in s1_arrays:
        test("bge-m3 vs qwen3-4b (translated)", "bge-m3/translated", "qwen3-4b/translated")

    # rerank: bge-m3 translated bi-encoder vs + cross-encoder over top-20
    try:
        from sentence_transformers import CrossEncoder

        ce = CrossEncoder("BAAI/bge-reranker-v2-m3", device=embedders._device(),
                          trust_remote_code=True)
        rer = np.zeros(len(products))
        for i in range(len(products)):
            cand = topk20[i]
            scores = ce.predict([(en_names[i], descs[j]) for j in cand], show_progress_bar=False)
            best = cand[int(np.argmax(scores))]
            rer[i] = food_labels.is_correct(descs[best], kws[i])
        test("bi-encoder vs +cross-encoder rerank", "bge-m3/translated", "_rer", b=rer)
    except Exception as e:  # noqa: BLE001
        print(f"  cross-encoder test skipped: {type(e).__name__}: {str(e)[:40]}")
    print("=" * 92)


if __name__ == "__main__":
    main()
