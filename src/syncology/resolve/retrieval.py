"""Retrieval scoring primitives for the systems matrix.

Pure functions shared by the retrieval ablation (dense vs sparse vs hybrid vs
late-interaction), kept here so they can be unit-tested away from the heavy encoders.
"""

from __future__ import annotations

import numpy as np


def ranks_from_scores(scores: np.ndarray) -> np.ndarray:
    """0-based rank position of every doc (rank 0 = highest score)."""
    order = np.argsort(-scores)
    r = np.empty(len(order), dtype=np.int64)
    r[order] = np.arange(len(order))
    return r


def rrf_fuse(rank_arrays: list[np.ndarray], k: int = 60) -> np.ndarray:
    """Reciprocal-rank fusion: sum 1/(k + rank) across systems (higher = better)."""
    return np.sum([1.0 / (k + r) for r in rank_arrays], axis=0)


def colbert_maxsim(
    q_tokens: np.ndarray, doc_tokens: np.ndarray, offsets: np.ndarray
) -> np.ndarray:
    """Late-interaction (ColBERT) MaxSim scores of one query against every doc.

    ``q_tokens`` is (Lq, D); all documents' token vectors are stacked into
    ``doc_tokens`` (Ntok, D) with per-doc start positions ``offsets`` (len Ndoc,
    plus a trailing sentinel = Ntok). Score(d) = Σ_i max_{j∈d} q_i·d_j — the sum
    over query tokens of the best-matching document token. Vectors are assumed
    L2-normalized (bge-m3 colbert vecs are), so the dot product is cosine.
    """
    sims = q_tokens @ doc_tokens.T  # (Lq, Ntok)
    seg_max = np.maximum.reduceat(sims, offsets[:-1], axis=1)  # (Lq, Ndoc)
    return seg_max.sum(axis=0)
