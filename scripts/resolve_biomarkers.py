"""Resolve raw lab test names to canonical biomarkers and materialize the map.

Builds three objects on the warehouse:
- ``biomarker_registry``  — the canonical biomarker vocabulary (key, EN name, category, unit).
- ``biomarker_map``       — raw ``test_name`` → canonical key, with the method used.
- ``lab_results_canonical`` — view joining lab_results to its canonical biomarker.

Uses the rule-based resolver; the LLM resolver + accuracy comparison is a
separate step. Prints coverage only (no health values).

Usage:
    uv run python scripts/resolve_biomarkers.py [--db DB_PATH]
"""

from __future__ import annotations

import argparse
from collections import Counter

from syncology import db
from syncology.resolve import reference_ranges
from syncology.resolve.biomarkers import REGISTRY, RuleResolver


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = ap.parse_args()

    con = db.connect(args.db)
    resolver = RuleResolver()

    # Registry table.
    con.execute("DROP TABLE IF EXISTS biomarker_registry")
    con.execute(
        """
        CREATE TABLE biomarker_registry (
            key       VARCHAR PRIMARY KEY,
            name_en   VARCHAR NOT NULL,
            category  VARCHAR NOT NULL,
            unit      VARCHAR,
            specimen  VARCHAR NOT NULL
        )
        """
    )
    con.executemany(
        "INSERT INTO biomarker_registry VALUES (?, ?, ?, ?, ?)",
        [(b.key, b.name_en, b.category, b.unit, "urine" if b.urine else "blood") for b in REGISTRY],
    )

    # Resolve each distinct (raw name, unit) and build the map.
    pairs = con.execute(
        "SELECT test_name, any_value(unit) FROM lab_results GROUP BY test_name"
    ).fetchall()
    con.execute("DROP TABLE IF EXISTS biomarker_map")
    con.execute(
        """
        CREATE TABLE biomarker_map (
            raw_name      VARCHAR PRIMARY KEY,
            canonical_key VARCHAR,
            method        VARCHAR NOT NULL,
            score         DOUBLE
        )
        """
    )
    methods: Counter[str] = Counter()
    rows = []
    for name, unit in pairs:
        res = resolver.resolve(name, unit)
        methods[res.method] += 1
        rows.append((name, res.key, res.method, res.score))
    con.executemany("INSERT INTO biomarker_map VALUES (?, ?, ?, ?)", rows)

    # Canonical view: every lab result tagged with its biomarker.
    con.execute(
        """
        CREATE OR REPLACE VIEW lab_results_canonical AS
        SELECT r.*, m.canonical_key, g.name_en AS biomarker, g.category, g.unit AS canonical_unit
        FROM lab_results r
        LEFT JOIN biomarker_map m ON r.test_name = m.raw_name
        LEFT JOIN biomarker_registry g ON m.canonical_key = g.key
        """
    )

    total = len(pairs)
    resolved = total - methods["none"]
    print("=" * 60)
    print("BIOMARKER RESOLUTION (rule-based)")
    print("=" * 60)
    print(f"registry biomarkers : {len(REGISTRY)}")
    print(f"raw names           : {total}")
    print(f"resolved            : {resolved}  ({resolved / total:.0%})")
    print(f"by method           : {dict(methods)}")

    print("\nBy category (canonical biomarkers / rows):")
    cats = con.execute(
        """
        SELECT category, count(DISTINCT canonical_key) AS biomarkers, count(*) AS rows
        FROM lab_results_canonical WHERE canonical_key IS NOT NULL
        GROUP BY category ORDER BY rows DESC
        """
    ).fetchall()
    for cat, nb, nr in cats:
        print(f"  {cat:<14} {nb:>3} biomarkers  {nr:>4} rows")

    # Cross-panel win: how many biomarkers now have a multi-panel time series?
    multi = con.execute(
        """
        SELECT count(*) FROM (
            SELECT canonical_key FROM lab_results_canonical
            WHERE canonical_key IS NOT NULL
            GROUP BY canonical_key HAVING count(DISTINCT panel_date) >= 3
        )
        """
    ).fetchone()[0]
    print(f"\nbiomarkers measured on >= 3 dates (trendable): {multi}")

    # Temporal reference ranges: a lab revises its intervals over time, so a
    # result is judged against the range in effect on its date.
    reference_ranges.build(con)
    changing = con.execute(
        """
        SELECT key, count(*) AS eras FROM biomarker_reference_ranges
        GROUP BY key HAVING eras > 1 ORDER BY eras DESC
        """
    ).fetchall()
    print(f"\nbiomarkers whose reference range CHANGED over time: {len(changing)}")
    for key, eras in changing:
        spans = con.execute(
            """
            SELECT ref_low, ref_high, valid_from, valid_to
            FROM biomarker_reference_ranges WHERE key = ? ORDER BY valid_from
            """,
            [key],
        ).fetchall()
        segs = "  ".join(
            f"[{lo}-{hi}] {vf}→{vt or 'now'}" for lo, hi, vf, vt in spans
        )
        print(f"  {key:<20} {eras} eras: {segs}")
    # Why it matters: results whose verdict would flip vs the current range.
    flips = con.execute(
        """
        WITH cur AS (
            SELECT key, ref_low, ref_high FROM biomarker_reference_ranges
            WHERE valid_to IS NULL
        )
        SELECT count(*) FROM lab_results_ranged r JOIN cur c ON r.canonical_key = c.key
        WHERE r.value_num IS NOT NULL AND r.asof_status IS NOT NULL
          AND r.asof_status <> (
              CASE WHEN r.value_num < c.ref_low THEN 'low'
                   WHEN c.ref_high IS NOT NULL AND r.value_num > c.ref_high THEN 'high'
                   ELSE 'normal' END)
        """
    ).fetchone()[0]
    print(f"results whose in-range verdict flips vs the current range: {flips}")
    print("=" * 60)
    con.close()


if __name__ == "__main__":
    main()
