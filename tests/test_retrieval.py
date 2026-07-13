"""Tests for the retrieval scoring primitives."""

from __future__ import annotations

import numpy as np

from syncology.resolve import retrieval


def test_ranks_from_scores():
    r = retrieval.ranks_from_scores(np.array([0.1, 0.9, 0.5]))
    assert list(r) == [2, 0, 1]  # doc1 best (rank 0), doc2 mid, doc0 worst


def test_rrf_fuse_rewards_consensus():
    # doc0: top in A, mid in B; doc1: mid in A, top in B; doc2: bottom in both
    ra = np.array([0, 1, 2])
    rb = np.array([1, 0, 2])
    fused = retrieval.rrf_fuse([ra, rb], k=60)
    assert np.argmax(fused) in (0, 1)  # a consensus doc, never the bottom one
    assert fused[2] == min(fused)


def test_colbert_maxsim_picks_token_overlap():
    # two docs: doc0 has a token equal to the query token; doc1 orthogonal
    q = np.array([[1.0, 0, 0, 0]])
    doc_tokens = np.array([
        [0.0, 1, 0, 0], [1.0, 0, 0, 0],   # doc0 (2 tokens, 2nd matches q)
        [0.0, 0, 1, 0],                    # doc1 (1 token, orthogonal)
    ])
    offsets = np.array([0, 2, 3])  # doc0=[0:2], doc1=[2:3]
    scores = retrieval.colbert_maxsim(q, doc_tokens, offsets)
    assert scores.shape == (2,)
    assert scores[0] == 1.0 and scores[1] == 0.0
