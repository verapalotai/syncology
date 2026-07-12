"""Tests for food reconciliation helpers (no network — embedding paths excluded)."""

from __future__ import annotations

import duckdb
import numpy as np

from syncology.resolve import foods


def test_normalize_strips_accents_and_punct():
    assert foods._norm("Extra szűz olívaolaj!") == "extra szuz olivaolaj"
    assert foods._norm("Répa, nyersen") == "repa nyersen"


def test_macro_vec_scaled_per_100g():
    v = foods._macro_vec(90, 1.0, 0.3, 23.0)  # banana-ish
    assert np.allclose(v, [90 / 900, 1.0 / 100, 0.3 / 100, 23.0 / 100])
    # None-safe
    assert np.allclose(foods._macro_vec(None, None, None, None), [0, 0, 0, 0])


def test_fuzzy_reconciler_baseline():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE foods(fdc_id BIGINT, description VARCHAR)")
    con.executemany(
        "INSERT INTO foods VALUES (?, ?)",
        [(1, "Banana, raw"), (2, "Apple, raw"), (3, "Chicken breast, cooked")],
    )
    r = foods.FuzzyReconciler(con, cutoff=0.5)
    # English name fuzzy-matches; a Hungarian name does not (the whole point)
    assert r.resolve("Banana raw").fdc_id == 1
    assert r.resolve("Zzzznonsense").fdc_id is None
