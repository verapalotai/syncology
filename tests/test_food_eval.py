"""Tests for the food-retrieval eval metrics + significance tests."""

from __future__ import annotations

import numpy as np

from syncology.resolve import food_eval, food_labels


def test_query_metrics_first_rank():
    # correct doc at rank 0 → all success flags true, MRR 1.0
    m = food_eval.query_metrics(food_eval.QueryOutcome(hit_ranks=[0], n_relevant=20))
    assert m["success@1"] == 1.0 and m["mrr"] == 1.0

    # first correct at rank 3 (0-based) → miss@1, hit@5, MRR 1/4
    m = food_eval.query_metrics(food_eval.QueryOutcome(hit_ranks=[3], n_relevant=20))
    assert m["success@1"] == 0.0 and m["success@5"] == 1.0
    assert np.isclose(m["mrr"], 0.25)

    # no hit → zeros
    m = food_eval.query_metrics(food_eval.QueryOutcome(hit_ranks=[], n_relevant=20))
    assert m["success@10"] == 0.0 and m["mrr"] == 0.0 and m["ndcg@10"] == 0.0


def test_bootstrap_ci_brackets_mean():
    vals = np.array([1.0] * 88 + [0.0] * 12)  # 0.88
    mean, lo, hi = food_eval.bootstrap_ci(vals, n_boot=2000, seed=1)
    assert np.isclose(mean, 0.88)
    assert lo < mean < hi and 0.80 < lo and hi < 0.95


def test_mcnemar_directional_and_symmetric():
    # a strictly dominates b (a right where b wrong, never the reverse)
    a = np.array([1, 1, 1, 1, 0])
    b = np.array([0, 0, 0, 1, 0])
    r = food_eval.mcnemar_exact(a, b)
    assert r["a_only"] == 3 and r["b_only"] == 0
    # identical systems → no discordance → p = 1
    assert food_eval.mcnemar_exact(a, a)["p_value"] == 1.0


def test_cascade_route_uses_usda_confidence_only():
    u_cos = np.array([0.9, 0.5, 0.6])
    u_ok = np.array([1, 0, 1])   # USDA correct on q0,q2; wrong on q1
    o_ok = np.array([0, 1, 0])   # OFF correct only on q1
    # tau=0.7: q0 stays USDA (conf), q1+q2 fall back to OFF
    out = food_eval.cascade_route(u_cos, u_ok, o_ok, 0.7)
    assert list(out) == [1, 1, 0]
    # tau=0.0: never fall back → pure USDA
    assert list(food_eval.cascade_route(u_cos, u_ok, o_ok, 0.0)) == [1, 0, 1]


def test_classify_strata():
    assert food_labels.classify("Cékla", "boiled beetroot") == "prepared"
    assert food_labels.classify("Piros alma", "red apple") == "compound"
    assert food_labels.classify("Oatly Barista", "oat drink") == "branded"
    assert food_labels.classify("Lecsó", "lecso") == "regional"
    assert food_labels.classify("Alma", "apple") == "simple"
