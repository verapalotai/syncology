"""Build the daily marts + cycle-phase table and print a summary.

Depends on the categorized measurements view (run normalize_categories first,
or just re-run this — it rebuilds the views it needs).

Usage:
    uv run python scripts/build_marts.py [--db DB_PATH] [--tz IANA_ZONE]
"""

from __future__ import annotations

import argparse

from syncology import db
from syncology.transform import category_values as cv
from syncology.transform import marts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH, help="DuckDB warehouse path")
    ap.add_argument("--tz", default=marts.DEFAULT_TZ, help="local timezone for day bucketing")
    args = ap.parse_args()

    con = db.connect(args.db)
    cv.apply(con)  # ensure measurements_categorized exists (cycle_days depends on it)
    stats = marts.apply(con, tz=args.tz)

    print("=" * 56)
    print(f"DAILY MARTS  (day grain: {args.tz})")
    print("=" * 56)
    print(f"daily_activity  rows : {stats.daily_activity_rows:,}")
    print(f"daily_nutrition rows : {stats.daily_nutrition_rows:,}")
    print(f"cycle_days      rows : {stats.cycle_days_rows:,}")

    print("\ncycle_phases distribution:")
    order = ["menstruation", "follicular", "ovulation", "luteal", "unknown"]
    total = sum(stats.phase_counts.values()) or 1
    for phase in order:
        n = stats.phase_counts.get(phase, 0)
        print(f"  {phase:<14} {n:>5}  ({n / total:5.1%})")
    fertile = con.execute("SELECT count(*) FROM cycle_phases WHERE fertile_window").fetchone()[0]
    print(f"  fertile_window days: {fertile}")

    print(f"\ncycle_summary  (coverline shift_c = {stats.shift_c} °C):")
    print(f"  cycles              : {stats.n_cycles}")
    print(f"  ovulatory (detected): {stats.n_ovulatory_cycles}"
          f"  ({stats.n_ovulatory_cycles / (stats.n_cycles or 1):.0%})")
    print(f"  suspect (guard hit) : {stats.n_suspect_cycles}"
          f"   (false-early shift skipped: implied luteal > {marts._MAX_LUTEAL_DAYS}d)")
    print(f"  flagged vs norms    : {stats.n_flagged_cycles}")
    flagged = con.execute(
        """
        SELECT cycle_number, cycle_start, cycle_length_days, round(cycle_length_z, 1) AS cz,
               luteal_days, round(luteal_length_z, 1) AS lz, short_luteal
        FROM cycle_summary
        WHERE cycle_length_flag OR luteal_length_flag
        ORDER BY cycle_start
        """
    ).fetchall()
    for num, start, length, cz, luteal, lz, short in flagged:
        reasons = []
        if cz is not None and abs(cz) > marts.FLAG_SIGMA:
            reasons.append(f"cycle_len {length}d z={cz:+}")
        if lz is not None and abs(lz) > marts.FLAG_SIGMA:
            reasons.append(f"luteal {luteal}d z={lz:+}" + (" SHORT" if short else ""))
        print(f"    cycle {num:<2} {start}  " + "; ".join(reasons))

    print("\nExample cross-domain query — avg BBT by cycle phase:")
    rows = con.execute(
        """
        SELECT p.phase, count(d.bbt_c) AS n_days, round(avg(d.bbt_c), 3) AS avg_bbt_c
        FROM cycle_phases p JOIN cycle_days d USING (day)
        WHERE d.bbt_c IS NOT NULL
        GROUP BY p.phase ORDER BY avg_bbt_c NULLS LAST
        """
    ).fetchall()
    for phase, n, avg in rows:
        print(f"  {phase:<14} n={n:<4} avg_bbt={avg}")
    print("=" * 56)
    con.close()


if __name__ == "__main__":
    main()
