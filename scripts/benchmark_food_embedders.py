"""Benchmark embedding models for cross-lingual food entity resolution.

Compares open embedders (bge-m3, Qwen3-Embedding-*, harrier) on retrieving the
correct USDA food for a hand-labeled set of Hungarian Yazio foods, across two
query transforms (raw name vs English translation) and two rerank steps (macro
fingerprint, cross-encoder). Metrics: recall@{1,5,10} + MRR@10.

Grounding: retrieve-then-rerank entity linking (BLINK, Wu et al. 2020),
attribute-aware matching (Ditto, Li et al. 2020), instruction-tuned query
encoding (Qwen3-Embedding, 2025). Public foods + food-category labels only.

Usage:
    uv run python scripts/benchmark_food_embedders.py [--models a,b,c] [--rerank]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb
import numpy as np

from syncology import db
from syncology.resolve import embedders
from syncology.resolve.food_labels import is_correct, load_gold

CACHE = Path("data/clean")
_MACRO_SCALE = np.array([900.0, 100.0, 100.0, 100.0], dtype=np.float32)


def _macro(rows) -> np.ndarray:
    v = np.array([[r or 0.0 for r in row] for row in rows], dtype=np.float32)
    return v / _MACRO_SCALE


def _metrics(sims: np.ndarray, descs: list[str], gold_kw: list[tuple], k=10) -> dict:
    r1 = r5 = r10 = 0
    rr = 0.0
    topk = np.argsort(-sims, axis=1)[:, :k]
    for i, kw in enumerate(gold_kw):
        hits = [j for j, idx in enumerate(topk[i]) if is_correct(descs[idx], kw)]
        if hits:
            first = hits[0]
            rr += 1.0 / (first + 1)
            r1 += first == 0
            r5 += first < 5
            r10 += first < 10
    n = len(gold_kw)
    return {"R@1": r1 / n, "R@5": r5 / n, "R@10": r10 / n, "MRR": rr / n}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="bge-m3,qwen3-0.6b,qwen3-4b,harrier-0.6b")
    ap.add_argument("--rerank", action="store_true", help="also run rerank ablation")
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()

    gold_labels = load_gold()

    con = duckdb.connect(args.db, read_only=True)
    corpus = con.execute(
        "SELECT fdc_id, description, energy_kcal, protein_g, fat_g, carbs_g FROM foods"
    ).fetchall()
    descs = [r[1] for r in corpus]
    doc_macros = _macro([r[2:6] for r in corpus])

    gold = con.execute(
        f"""
        SELECT y.product, m.en_name, y.energy_kcal, y.protein_g, y.fat_g, y.carbs_g
        FROM yazio_foods y JOIN food_map m USING(product)
        WHERE y.product IN ({', '.join('?' for _ in gold_labels)})
        """,
        list(gold_labels),
    ).fetchall()
    con.close()
    products = [g[0] for g in gold]
    en_names = [g[1] or g[0] for g in gold]
    q_macros = _macro([g[2:6] for g in gold])
    gold_kw = [gold_labels[p] for p in products]

    print("=" * 78)
    print(f"FOOD EMBEDDER BENCHMARK   gold={len(gold)}  corpus={len(descs):,}")
    print("=" * 78)
    print(f"{'model':<14}{'size':>5}{'query':>12}{'R@1':>7}{'R@5':>7}{'R@10':>7}{'MRR':>7}{'embed_s':>9}")

    best = None
    for name in args.models.split(","):
        name = name.strip()
        try:
            emb = embedders.Embedder(embedders.SPECS[name])
        except Exception as e:  # noqa: BLE001
            print(f"{name:<14} LOAD FAILED: {type(e).__name__}: {str(e)[:40]}")
            continue
        cache = CACHE / f"bench_docemb_{name}.npy"
        t0 = time.perf_counter()
        if cache.exists():
            doc_emb = np.load(cache)
        else:
            doc_emb = emb.encode_docs(descs)
            np.save(cache, doc_emb)
        embed_s = time.perf_counter() - t0
        for qlabel, qtexts in (("raw", products), ("translated", en_names)):
            q_emb = emb.encode_queries(qtexts)
            sims = q_emb @ doc_emb.T
            m = _metrics(sims, descs, gold_kw)
            print(f"{name:<14}{emb.spec.params:>5}{qlabel:>12}"
                  f"{m['R@1']:>7.2f}{m['R@5']:>7.2f}{m['R@10']:>7.2f}{m['MRR']:>7.2f}"
                  f"{embed_s:>9.0f}")
            if qlabel == "translated" and (best is None or m["R@5"] > best[3]["R@5"]):
                best = (name, q_emb, doc_emb, m)
        del emb

    if args.rerank and best is not None:
        name, q_emb, doc_emb, base = best
        sims = q_emb @ doc_emb.T
        print("-" * 78)
        print(f"RERANK ABLATION  (best embedder = {name}, translated query, top-20)")
        topn = np.argsort(-sims, axis=1)[:, :20]
        # bi-encoder only
        print(f"  bi-encoder only         R@1={base['R@1']:.2f}")
        # + macro rerank
        r1 = 0
        for i in range(len(gold)):
            cand = topn[i]
            md = np.linalg.norm(doc_macros[cand] - q_macros[i], axis=1)
            combined = 0.5 * sims[i][cand] + 0.5 * (1.0 / (1.0 + md))
            best_idx = cand[int(np.argmax(combined))]
            r1 += is_correct(descs[best_idx], gold_kw[i])
        print(f"  + macro rerank          R@1={r1 / len(gold):.2f}")
        # + cross-encoder rerank
        try:
            from sentence_transformers import CrossEncoder

            ce = CrossEncoder("BAAI/bge-reranker-v2-m3", device=embedders._device(),
                              trust_remote_code=True)
            r1 = 0
            for i in range(len(gold)):
                cand = topn[i]
                pairs = [(en_names[i], descs[j]) for j in cand]
                scores = ce.predict(pairs, show_progress_bar=False)
                best_idx = cand[int(np.argmax(scores))]
                r1 += is_correct(descs[best_idx], gold_kw[i])
            print(f"  + cross-encoder rerank  R@1={r1 / len(gold):.2f}  (bge-reranker-v2-m3)")
        except Exception as e:  # noqa: BLE001
            print(f"  cross-encoder rerank skipped: {type(e).__name__}: {str(e)[:50]}")
    print("=" * 78)


if __name__ == "__main__":
    main()
