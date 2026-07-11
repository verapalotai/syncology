"""Build the unified ``activities`` table from Strava + Apple workouts.

Prints counts and type distribution only (no personal values). Also creates a
``daily_activity_events`` view — per-day counts/totals of discrete activities —
so the graph and marts can link an Activity to its Day.

Usage:
    uv run python scripts/build_activities.py [--db DB] [--strava CSV]
"""

from __future__ import annotations

import argparse
import os

from syncology import db
from syncology.ingest import activities


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument(
        "--strava",
        default=os.path.join(
            os.environ.get("SYNCOLOGY_DATA_DIR", "data"),
            "raw/personal/activity/strava_activities.csv",
        ),
    )
    args = ap.parse_args()

    con = db.connect(args.db)
    counts = activities.build(con, args.strava)

    con.execute(
        """
        CREATE OR REPLACE VIEW daily_activity_events AS
        SELECT CAST(start_ts AT TIME ZONE 'Europe/Budapest' AS DATE) AS day,
            count(*)                 AS n_activities,
            sum(duration_s) / 60.0   AS active_minutes,
            sum(distance_km)         AS distance_km,
            string_agg(DISTINCT activity_type, ', ') AS activity_types
        FROM activities GROUP BY 1 ORDER BY day
        """
    )

    total = con.execute("SELECT count(*) FROM activities").fetchone()[0]
    lo, hi = con.execute("SELECT min(start_ts), max(start_ts) FROM activities").fetchone()
    print("=" * 60)
    print("ACTIVITIES")
    print("=" * 60)
    print(f"loaded: {counts} | total rows: {total}")
    print(f"date range: {str(lo)[:10]} -> {str(hi)[:10]}")
    print("\nBy type:")
    for atype, n, km in con.execute(
        """
        SELECT activity_type, count(*), round(sum(distance_km), 0)
        FROM activities GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall():
        print(f"  {atype:<14} {n:>4}   {km or 0:>6.0f} km total")
    days = con.execute("SELECT count(*) FROM daily_activity_events").fetchone()[0]
    print(f"\ndistinct activity days: {days}")
    print("=" * 60)
    con.close()


if __name__ == "__main__":
    main()
