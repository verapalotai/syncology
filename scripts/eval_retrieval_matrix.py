"""Retrieval-systems matrix — does retrieval *sophistication* move the needle?

Holds the corpus fixed (USDA) and the embedder fixed (bge-m3), and varies only the
retrieval method: dense single-vector (current), sparse BM25, hybrid dense+BM25 via
reciprocal-rank fusion, and bge-m3 late-interaction (ColBERT MaxSim). Reports
Success@1 with bootstrap 95% CIs, Success@5, MRR, nDCG@10, a per-stratum Success@1
split, and McNemar tests of each system against dense — for raw and translated
queries. The question the rest of the report sets up: on short food names, is the
leverage in the retrieval algorithm, or (as translation and coverage suggested)
elsewhere?

Usage:
    SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
      uv run python scripts/eval_retrieval_matrix.py
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import duckdb
import numpy as np

from syncology import db
from syncology.resolve import food_eval, food_labels, retrieval

K = 10
STRATA = ("simple", "compound", "prepared", "branded", "regional")
_CACHE = Path("data/clean")


def _tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _encode_corpus(model, descs):
    """Dense (Ndoc,1024) + flattened colbert tokens + offsets, cached to disk."""
    dense_c = _CACHE / "matrix_dense_bge-m3.npy"
    col_c = _CACHE / "matrix_colbert_bge-m3.npz"
    if dense_c.exists() and col_c.exists():
        z = np.load(col_c)
        if len(np.load(dense_c)) == len(descs) and len(z["offsets"]) == len(descs) + 1:
            return np.load(dense_c), z["tokens"], z["offsets"]
    out = model.encode(descs, batch_size=64, return_dense=True, return_colbert_vecs=True)
    dense = np.asarray(out["dense_vecs"], dtype=np.float32)
    cvs = out["colbert_vecs"]
    offsets = np.concatenate([[0], np.cumsum([len(c) for c in cvs])]).astype(np.int64)
    tokens = np.concatenate(cvs).astype(np.float16)
    np.save(dense_c, dense)
    np.savez(col_c, tokens=tokens, offsets=offsets)
    return dense, tokens, offsets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()
    from FlagEmbedding import BGEM3FlagModel
    from rank_bm25 import BM25Okapi

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

    # n_relevant per query (for nDCG ideal), precomputed corpus stems
    corpus_stems = [food_labels._stems(d) for d in descs]
    n_rel = [sum(1 for ds in corpus_stems if any(ks <= ds for ks in
             [food_labels._stems(k) for k in kw])) for kw in kws]

    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True, devices="mps")
    doc_dense, doc_tokens, offsets = _encode_corpus(model, descs)
    doc_tokens_f = doc_tokens.astype(np.float32)
    bm25 = BM25Okapi([_tok(d) for d in descs])

    def outcomes_from_topk(topk):
        outs = []
        for i in range(len(products)):
            hits = [j for j, d in enumerate(topk[i]) if food_labels.is_correct(descs[d], kws[i])]
            outs.append(food_eval.QueryOutcome(hit_ranks=hits, n_relevant=n_rel[i]))
        return outs

    for qlabel, texts in (("translated", en_names), ("raw", products)):
        enc = model.encode(texts, return_dense=True, return_colbert_vecs=True)
        q_dense = np.asarray(enc["dense_vecs"], dtype=np.float32)
        q_col = enc["colbert_vecs"]
        dense_scores = q_dense @ doc_dense.T  # (Q, Ndoc)

        rankings: dict[str, np.ndarray] = {}  # system -> (Q, K) top-k doc idx
        s1_full: dict[str, np.ndarray] = {}   # system -> (Q,) success@1 bool
        for name in ("dense", "sparse", "hybrid", "colbert"):
            topk = np.empty((len(texts), K), dtype=np.int64)
            for i in range(len(texts)):
                if name == "dense":
                    sc = dense_scores[i]
                elif name == "sparse":
                    sc = bm25.get_scores(_tok(texts[i]))
                elif name == "hybrid":
                    rd = retrieval.ranks_from_scores(dense_scores[i])
                    rs = retrieval.ranks_from_scores(bm25.get_scores(_tok(texts[i])))
                    sc = retrieval.rrf_fuse([rd, rs])
                else:  # colbert
                    sc = retrieval.colbert_maxsim(
                        q_col[i].astype(np.float32), doc_tokens_f, offsets)
                topk[i] = np.argsort(-sc)[:K]
            rankings[name] = topk
            s1_full[name] = np.array(
                [food_labels.is_correct(descs[topk[i][0]], kws[i]) for i in range(len(texts))])

        print("\n" + "=" * 96)
        print(f"QUERY = {qlabel}   (USDA corpus, bge-m3)   N={len(texts)}")
        print(f"{'system':<9}{'Success@1 [95% CI]':>24}{'S@5':>8}{'MRR':>8}{'nDCG@10':>9}"
              f"{'  vs dense (McNemar)':>22}")
        print("-" * 96)
        for name in ("dense", "sparse", "hybrid", "colbert"):
            arr = food_eval.aggregate(outcomes_from_topk(rankings[name]))
            m, lo, hi = food_eval.bootstrap_ci(arr["success@1"])
            cmp = ""
            if name != "dense":
                r = food_eval.mcnemar_exact(s1_full[name], s1_full["dense"])
                cmp = f"+{r['a_only']}/-{r['b_only']} p={r['p_value']:.3g}"
            print(f"{name:<9}{f'{m:.3f} [{lo:.3f},{hi:.3f}]':>24}{arr['success@5'].mean():>8.3f}"
                  f"{arr['mrr'].mean():>8.3f}{arr['ndcg@10'].mean():>9.3f}{cmp:>22}")

        print(f"per-stratum Success@1 ({qlabel}):  " + "  ".join(f"{s}(N={int((strata==s).sum())})" for s in STRATA))
        for name in ("dense", "sparse", "hybrid", "colbert"):
            cells = "  ".join(f"{s1_full[name][strata==s].mean():.2f}" if (strata==s).any() else " - " for s in STRATA)
            print(f"  {name:<9} {cells}")


if __name__ == "__main__":
    main()
