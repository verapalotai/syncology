"""Tests for temporal (as-of) biomarker reference ranges."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from syncology.resolve import reference_ranges


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    # Minimal stand-in for the lab_results_canonical view.
    c.execute(
        """
        CREATE TABLE lab_results_canonical (
            canonical_key VARCHAR, panel_date DATE, value_num DOUBLE,
            ref_low DOUBLE, ref_high DOUBLE, canonical_unit VARCHAR
        )
        """
    )
    rows = [
        # biomarker 'x': range [10,20] then revised to [12,22] mid-2022
        ("x", dt.date(2022, 1, 1), 11.0, 10.0, 20.0, "u"),
        ("x", dt.date(2022, 2, 1), 15.0, 10.0, 20.0, "u"),
        ("x", dt.date(2022, 6, 1), 15.0, 12.0, 22.0, "u"),
        ("x", dt.date(2022, 7, 1), 11.0, 12.0, 22.0, "u"),
        # biomarker 'y': range recurs (interleaved) -> 3 reigns
        ("y", dt.date(2021, 1, 1), 1.5, 1.0, 2.0, "u"),
        ("y", dt.date(2021, 2, 1), 3.5, 3.0, 4.0, "u"),
        ("y", dt.date(2021, 3, 1), 1.5, 1.0, 2.0, "u"),
    ]
    c.executemany("INSERT INTO lab_results_canonical VALUES (?,?,?,?,?,?)", rows)
    reference_ranges.build(c)
    yield c
    c.close()


def test_eras_and_validity_windows(con):
    eras = con.execute(
        "SELECT ref_low, ref_high, valid_from, valid_to FROM biomarker_reference_ranges "
        "WHERE key = 'x' ORDER BY valid_from"
    ).fetchall()
    assert eras == [
        (10.0, 20.0, dt.date(2022, 1, 1), dt.date(2022, 5, 31)),  # closed at day before next
        (12.0, 22.0, dt.date(2022, 6, 1), None),                  # current era: open-ended
    ]


def test_interleaved_range_gets_separate_reigns(con):
    n = con.execute(
        "SELECT count(*) FROM biomarker_reference_ranges WHERE key = 'y'"
    ).fetchone()[0]
    assert n == 3  # [1,2], [3,4], [1,2] again — three contiguous reigns


def test_asof_returns_range_in_effect(con):
    assert reference_ranges.asof(con, "x", dt.date(2022, 3, 1))[:2] == (10.0, 20.0)
    assert reference_ranges.asof(con, "x", dt.date(2022, 7, 15))[:2] == (12.0, 22.0)


def test_evaluate_verdict_flips_across_eras(con):
    # value 11 is normal under the old [10,20] range, but low under the new [12,22]
    assert reference_ranges.evaluate(con, "x", dt.date(2022, 3, 1), 11.0) == "normal"
    assert reference_ranges.evaluate(con, "x", dt.date(2022, 7, 15), 11.0) == "low"


def test_ranged_view_flags_asof_status(con):
    # the two value=11 rows: normal in era1 (2022-01-01), low in era2 (2022-07-01)
    got = dict(
        con.execute(
            "SELECT panel_date, asof_status FROM lab_results_ranged "
            "WHERE canonical_key = 'x' AND value_num = 11.0"
        ).fetchall()
    )
    assert got[dt.date(2022, 1, 1)] == "normal"
    assert got[dt.date(2022, 7, 1)] == "low"


def test_evaluate_unknown_biomarker_is_none(con):
    assert reference_ranges.evaluate(con, "nonexistent", dt.date(2022, 1, 1), 5.0) is None
