"""Build the canonical ``foods`` lookup table from USDA FoodData Central.

Public reference data (no personal values). Prints coverage and a nutrient-fill
summary.

Usage:
    uv run python scripts/build_foods.py [--db DB] [--fdc DIR]
"""

from __future__ import annotations

import argparse
import os
import time

from syncology import db
from syncology.ingest import nutrients


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument(
        "--fdc",
        default=os.path.join(
            os.environ.get("SYNCOLOGY_DATA_DIR", "data"),
            "raw/public/fooddata_central",
        ),
    )
    args = ap.parse_args()

    con = db.connect(args.db)
    t0 = time.perf_counter()
    n = nutrients.build(con, args.fdc)
    dt = time.perf_counter() - t0

    print("=" * 60)
    print("FOODS (USDA FoodData Central)")
    print("=" * 60)
    print(f"foods rows: {n:,}   (built in {dt:.0f}s)")

    print("\nBy data_type:")
    for t, c in con.execute(
        "SELECT data_type, count(*) FROM foods GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {t:<20} {c:>7,}")

    print("\nNutrient coverage (non-null of total):")
    for _nid, (col, unit) in nutrients.NUTRIENTS.items():
        filled = con.execute(f"SELECT count({col}) FROM foods").fetchone()[0]
        print(f"  {col:<18} {filled:>7,}  ({filled / (n or 1):.0%})  [{unit}]")
    print("=" * 60)
    con.close()


if __name__ == "__main__":
    main()
