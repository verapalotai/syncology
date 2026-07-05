"""Parse the Apple Health export into DuckDB and print a verification report.

Usage:
    uv run python scripts/parse_apple_health.py [XML_PATH] [--db DB_PATH]

Defaults come from the SYNCOLOGY_DATA_DIR / SYNCOLOGY_DUCKDB_PATH environment
variables (see .env.example).
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from syncology import db
from syncology.ingest import apple_health

DEFAULT_XML = os.path.join(
    os.environ.get("SYNCOLOGY_DATA_DIR", "data"),
    "raw/personal/activity/apple_health_export/exportación.xml",
)

# Aggregate counts used only to flag drift. Values are counts of records, never
# health measurements themselves. These are UNIQUE-row counts, i.e. after
# collapsing the exact-duplicate Record elements the export contains (Yazio in
# particular writes each dietary record 2-4x, byte-for-byte identical). The raw
# element count is ~542k; deduping to the natural key leaves ~461k. Compare the
# report's "records seen" against the raw figure and everything else against
# these deduped baselines.
EXPECTED = {
    "raw_records": 542_000,
    "total_rows": 461_000,
    "metric_types": 59,
    "sources": {
        "Veronika’s iPhone": 372_362,
        "Yazio": 87_848,
        "Tempdrop": 769,
        "Slopes": 77,
        "Salud": 4,
    },
    "metric_spotcheck": {
        "BasalBodyTemperature": 585,
        "CervicalMucusQuality": 104,
        "MenstrualFlow": 60,
        "DietaryEnergyConsumed": 6_052,
        "DietaryProtein": 6_047,
        "DietaryCarbohydrates": 6_052,
        "DietaryFatTotal": 6_047,
    },
    "activity_summaries": 722,
    "date_range": ("2021-12-24", "2026-06-24"),
}


def _rebuild_catalog(con) -> None:
    """(Re)build a per-metric summary table for quick inspection."""
    con.execute("DROP TABLE IF EXISTS metric_catalog")
    con.execute(
        """
        CREATE TABLE metric_catalog AS
        SELECT
            metric,
            any_value(record_kind)              AS record_kind,
            count(*)                            AS n_rows,
            count(DISTINCT source)              AS n_sources,
            min(start_ts)                       AS first_ts,
            max(start_ts)                       AS last_ts,
            string_agg(DISTINCT unit, ', ')     AS units
        FROM measurements
        GROUP BY metric
        ORDER BY n_rows DESC
        """
    )


def _fmt(actual: int, expected: int, *, tol: float = 0.02) -> str:
    if expected == 0:
        return "OK" if actual == 0 else "CHECK"
    delta = abs(actual - expected) / expected
    return "OK" if delta <= tol else f"DRIFT ({delta:+.1%})"


def _report(con, stats: apple_health.ParseStats) -> None:
    total = con.execute("SELECT count(*) FROM measurements").fetchone()[0]
    n_types = con.execute("SELECT count(DISTINCT metric) FROM measurements").fetchone()[0]
    dmin, dmax = con.execute(
        "SELECT min(start_ts), max(start_ts) FROM measurements"
    ).fetchone()
    acts = con.execute("SELECT count(*) FROM activity_summary").fetchone()[0]

    print("\n" + "=" * 60)
    print("PARSE REPORT")
    print("=" * 60)
    print(f"Records seen (raw)      : {stats.records_seen:>10,}   {_fmt(stats.records_seen, EXPECTED['raw_records'])}")
    print(f"Rows inserted (this run): {stats.rows_inserted:,}")
    print(f"Correlations            : {stats.correlations:,}")
    print(f"Rows linked to a corr.  : {stats.correlated_rows:,}")
    print(f"Workouts                : {stats.workouts:,}")
    print()
    print(f"measurements rows       : {total:>10,}   {_fmt(total, EXPECTED['total_rows'])}")
    print(f"distinct metric types   : {n_types:>10,}   {_fmt(n_types, EXPECTED['metric_types'])}")
    print(f"activity_summary rows   : {acts:>10,}   {_fmt(acts, EXPECTED['activity_summaries'])}")
    exp_lo, exp_hi = EXPECTED["date_range"]
    print(f"date range              : {str(dmin)[:10]} -> {str(dmax)[:10]}   (expected {exp_lo} -> {exp_hi})")

    print("\nPer-source counts:")
    rows = con.execute(
        "SELECT source, count(*) FROM measurements GROUP BY source ORDER BY count(*) DESC"
    ).fetchall()
    for source, n in rows:
        exp = EXPECTED["sources"].get(source)
        flag = _fmt(n, exp) if exp is not None else ""
        print(f"  {source:<22} {n:>10,}   {flag}")

    print("\nMetric spot-checks:")
    for metric, exp in EXPECTED["metric_spotcheck"].items():
        n = con.execute(
            "SELECT count(*) FROM measurements WHERE metric = ?", [metric]
        ).fetchone()[0]
        print(f"  {metric:<26} {n:>8,}   {_fmt(n, exp)}")
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xml_path", nargs="?", default=DEFAULT_XML, help="Apple Health export XML")
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH, help="DuckDB warehouse path")
    args = ap.parse_args()

    xml_path = Path(args.xml_path)
    if not xml_path.exists():
        raise SystemExit(f"XML not found: {xml_path}")

    print(f"Parsing {xml_path} ({xml_path.stat().st_size / 1e6:.0f} MB) -> {args.db}")
    con = db.connect(args.db)
    started = time.perf_counter()
    stats = apple_health.parse(xml_path, con)
    _rebuild_catalog(con)
    elapsed = time.perf_counter() - started
    _report(con, stats)
    print(f"\nDone in {elapsed:.1f}s")
    con.close()


if __name__ == "__main__":
    main()
