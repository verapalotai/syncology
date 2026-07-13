"""Evaluation metrics + significance tests for the food-retrieval benchmark.

The task is concept-level *known-item* retrieval: each query has one correct food
concept, realized as a *set* of acceptable USDA rows (all "apple, raw" variants).
The task-appropriate metrics are therefore **Success@k** (a correct row in the
top k) and **MRR** (reciprocal rank of the first correct row). We add **nDCG@10**
as a graded supplement. We deliberately do *not* report MAP / R-precision: those
score retrieval of the *whole* relevant set, which here means ranking every "apple"
row high — not the goal, and misleading.

Significance: per-query outcomes let us bootstrap 95% CIs on every metric and run
**McNemar's exact test** for paired Success@1 comparisons between two systems.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class QueryOutcome:
    """Per-query retrieval outcome over the ranked top-k."""

    hit_ranks: list[int]  # 0-based ranks (within top-k) of relevant docs
    n_relevant: int  # total relevant docs in the corpus (for nDCG's ideal)


def query_metrics(o: QueryOutcome, k: int = 10) -> dict[str, float]:
    first = o.hit_ranks[0] if o.hit_ranks else None
    dcg = sum(1.0 / math.log2(r + 2) for r in o.hit_ranks if r < k)
    ideal_n = min(k, o.n_relevant)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_n)) or 1.0
    return {
        "success@1": float(first == 0) if first is not None else 0.0,
        "success@5": float(first is not None and first < 5),
        "success@10": float(first is not None and first < 10),
        "mrr": 1.0 / (first + 1) if first is not None else 0.0,
        "ndcg@10": dcg / idcg,
    }


METRICS = ("success@1", "success@5", "success@10", "mrr", "ndcg@10")


def aggregate(outcomes: list[QueryOutcome]) -> dict[str, np.ndarray]:
    """Return per-query value arrays keyed by metric (for bootstrapping)."""
    rows = [query_metrics(o) for o in outcomes]
    return {m: np.array([r[m] for r in rows], dtype=np.float64) for m in METRICS}


def bootstrap_ci(
    values: np.ndarray, n_boot: int = 10_000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float]:
    """Mean and a percentile bootstrap CI over per-query values."""
    rng = np.random.default_rng(seed)
    n = len(values)
    if n == 0:
        return (float("nan"),) * 3
    idx = rng.integers(0, n, size=(n_boot, n))
    means = values[idx].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(values.mean()), float(lo), float(hi)


def cascade_route(
    usda_cos: np.ndarray, usda_ok: np.ndarray, off_ok: np.ndarray, tau: float
) -> np.ndarray:
    """Confidence-routed cascade Success@1 (per query).

    Trust the USDA top-1 whenever its cosine ≥ ``tau``; fall back to the OFF top-1
    only when USDA is unconfident. Routing depends on USDA confidence *alone* — not
    on OFF's score — which is what protects the confident head from OFF's
    spuriously-high exact-string matches.
    """
    return np.where(np.asarray(usda_cos) >= tau, np.asarray(usda_ok), np.asarray(off_ok))


def mcnemar_exact(correct_a: np.ndarray, correct_b: np.ndarray) -> dict[str, float]:
    """Paired exact McNemar test on Success@1 (binary) outcomes of two systems.

    Returns the discordant counts and a two-sided exact (binomial) p-value — the
    right test for the *same* queries scored by two systems.
    """
    a = correct_a.astype(bool)
    b = correct_b.astype(bool)
    n_a_only = int(np.sum(a & ~b))  # a right, b wrong
    n_b_only = int(np.sum(~a & b))  # b right, a wrong
    n = n_a_only + n_b_only
    if n == 0:
        p = 1.0
    else:
        k = min(n_a_only, n_b_only)
        tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
        p = min(1.0, 2 * tail)
    return {"a_only": n_a_only, "b_only": n_b_only, "p_value": p}
