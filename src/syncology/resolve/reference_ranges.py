"""Temporal reference ranges — a lab's interval for a biomarker changes over time.

Labs revise their reference intervals (new method, updated population norms), so
a result must be judged against the range that was *in effect on its date*, not a
single "current" range. On the real data SHBG's interval moved 27.8→32.4→17.7 and
AMH shifted in 2023, so the same value can read "low" under one era and "normal"
under another.

This module derives a validity interval ``[valid_from, valid_to]`` per distinct
range (change-point / "reign" segmentation of the observed panels) into
``biomarker_reference_ranges``, and exposes an as-of lookup so any (biomarker,
date, value) can be evaluated against the correct historical range.
"""

from __future__ import annotations

import datetime as dt

import duckdb

# One row per contiguous "reign" of a reference interval per biomarker. A new
# reign starts whenever the printed (low, high) changes between consecutive
# panels, so an interval that recurs later gets its own reign (interleaving-safe).
_TIMELINE_SQL = """
CREATE OR REPLACE TABLE biomarker_reference_ranges AS
WITH panels AS (
    SELECT DISTINCT canonical_key AS key, panel_date, ref_low, ref_high,
           canonical_unit AS unit
    FROM lab_results_canonical
    WHERE canonical_key IS NOT NULL AND (ref_low IS NOT NULL OR ref_high IS NOT NULL)
),
changed AS (
    SELECT *,
        CASE WHEN (coalesce(ref_low, -999) || '/' || coalesce(ref_high, -999)) IS DISTINCT FROM
             lag(coalesce(ref_low, -999) || '/' || coalesce(ref_high, -999))
               OVER (PARTITION BY key ORDER BY panel_date)
        THEN 1 ELSE 0 END AS chg
    FROM panels
),
reigns AS (
    SELECT *, sum(chg) OVER (PARTITION BY key ORDER BY panel_date) AS reign
    FROM changed
),
agg AS (
    SELECT key, reign, any_value(ref_low) AS ref_low, any_value(ref_high) AS ref_high,
           any_value(unit) AS unit, min(panel_date) AS valid_from,
           max(panel_date) AS last_seen, count(*) AS n_panels
    FROM reigns GROUP BY key, reign
)
SELECT key, ref_low, ref_high, unit, n_panels, valid_from, last_seen,
       CAST(lead(valid_from) OVER (PARTITION BY key ORDER BY valid_from)
            - INTERVAL 1 DAY AS DATE) AS valid_to
FROM agg
ORDER BY key, valid_from
"""

# Each result tagged with the range in effect on its date + an in-range verdict.
_RANGED_VIEW_SQL = """
CREATE OR REPLACE VIEW lab_results_ranged AS
SELECT r.*, rr.ref_low AS asof_low, rr.ref_high AS asof_high,
    CASE
        WHEN r.value_num IS NULL OR rr.ref_low IS NULL THEN NULL
        WHEN r.value_num < rr.ref_low THEN 'low'
        WHEN rr.ref_high IS NOT NULL AND r.value_num > rr.ref_high THEN 'high'
        ELSE 'normal'
    END AS asof_status
FROM lab_results_canonical r
LEFT JOIN biomarker_reference_ranges rr
       ON r.canonical_key = rr.key
      AND r.panel_date >= rr.valid_from
      AND (rr.valid_to IS NULL OR r.panel_date <= rr.valid_to)
"""


def build(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize ``biomarker_reference_ranges`` and the ``lab_results_ranged`` view."""
    con.execute(_TIMELINE_SQL)
    con.execute(_RANGED_VIEW_SQL)


def asof(
    con: duckdb.DuckDBPyConnection, key: str, date: dt.date
) -> tuple[float | None, float | None, str | None] | None:
    """Return ``(low, high, unit)`` for the biomarker's range in effect on ``date``."""
    return con.execute(
        """
        SELECT ref_low, ref_high, unit FROM biomarker_reference_ranges
        WHERE key = ? AND valid_from <= ? AND (valid_to IS NULL OR ? <= valid_to)
        """,
        [key, date, date],
    ).fetchone()


def evaluate(
    con: duckdb.DuckDBPyConnection, key: str, date: dt.date, value: float
) -> str | None:
    """Classify a value against the range in effect on ``date``: low/normal/high/None."""
    r = asof(con, key, date)
    if not r or r[0] is None:
        return None
    low, high, _ = r
    if value < low:
        return "low"
    if high is not None and value > high:
        return "high"
    return "normal"
