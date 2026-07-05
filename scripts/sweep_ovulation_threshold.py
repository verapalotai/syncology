"""Sensitivity sweep for the BBT thermal-shift threshold.

Re-runs cycle analysis at several coverline offsets (°C) and reports how many
cycles turn ovulatory and how the phase mix shifts, so the default in
``marts.DEFAULT_SHIFT_C`` is chosen with eyes open rather than by fiat. Read-only
with respect to the persisted tables — it analyzes in memory and prints.

Usage:
    uv run python scripts/sweep_ovulation_threshold.py [--db DB_PATH]
"""

from __future__ import annotations

import argparse

from syncology import db
from syncology.transform import category_values as cv
from syncology.transform import marts

THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH, help="DuckDB warehouse path")
    args = ap.parse_args()

    con = db.connect(args.db)
    cv.apply(con)
    marts.build_cycle_days(con)
    rows = marts._fetch_cycle_days(con)

    # Cycle count is threshold-independent (menses-defined), so compute once.
    _, cycles0 = marts.analyze_cycles(rows, shift_c=marts.DEFAULT_SHIFT_C)
    total_cycles = len(cycles0)

    print("=" * 68)
    print(f"OVULATION-THRESHOLD SWEEP   ({total_cycles} cycles; STM course margin = 0.20)")
    print("  shift_c = required °C of the 3rd high above the coverline")
    print("=" * 68)
    print(f"{'shift_c':>8} {'ovulatory':>10} {'rate':>7} "
          f"{'ovul days':>9} {'follic':>7} {'luteal':>7} {'unknown':>8}")
    for t in THRESHOLDS:
        per_day, cycles = marts.analyze_cycles(rows, shift_c=t)
        n_ov = sum(1 for c in cycles if c.ovulation is not None)
        counts: dict[str, int] = {}
        for p in per_day:
            counts[p["phase"]] = counts.get(p["phase"], 0) + 1
        rate = n_ov / total_cycles if total_cycles else 0.0
        star = "  <- default" if abs(t - marts.DEFAULT_SHIFT_C) < 1e-9 else ""
        print(
            f"{t:>8.2f} {n_ov:>10} {rate:>6.0%} "
            f"{counts.get('ovulation', 0):>9} {counts.get('follicular', 0):>7} "
            f"{counts.get('luteal', 0):>7} {counts.get('unknown', 0):>8}{star}"
        )
    print("=" * 68)
    print("Lower threshold = more sensitive (more ovulations, fewer 'unknown'),")
    print("at the cost of more false shifts on noisy BBT. Higher = more conservative.")
    con.close()


if __name__ == "__main__":
    main()
