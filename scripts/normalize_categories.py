"""Normalize HealthKit category values and report coverage.

Builds ``category_value_map`` + the ``measurements_categorized`` view on the
warehouse, then prints per-label counts and flags any unmapped category value.
Run after the Apple Health parse.

Usage:
    uv run python scripts/normalize_categories.py [--db DB_PATH]
"""

from __future__ import annotations

import argparse

from syncology import db
from syncology.transform import category_values as cv


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH, help="DuckDB warehouse path")
    args = ap.parse_args()

    con = db.connect(args.db)
    stats = cv.apply(con)

    print("=" * 56)
    print("CATEGORY NORMALIZATION")
    print("=" * 56)
    print(f"mappings defined : {stats.mappings}")
    print(f"rows categorized : {stats.categorized_rows:,}")
    print(f"unmapped values  : {len(stats.unmapped)}")
    for metric, raw, n in stats.unmapped:
        print(f"  UNMAPPED  {metric} / {raw}  ({n})")

    print("\nPer-label counts (ordinal in brackets):")
    rows = con.execute(
        """
        SELECT metric, value_label, any_value(value_ordinal) AS ordinal, count(*) AS n
        FROM measurements_categorized
        WHERE value_label IS NOT NULL
        GROUP BY metric, value_label
        ORDER BY metric, ordinal NULLS LAST, n DESC
        """
    ).fetchall()
    cur = None
    for metric, label, ordinal, n in rows:
        if metric != cur:
            print(f"\n  {metric}:")
            cur = metric
        ord_s = "-" if ordinal is None else str(ordinal)
        print(f"    [{ord_s}] {label:<16} {n:>6,}")
    print("=" * 56)
    con.close()


if __name__ == "__main__":
    main()
