"""Build the ``off_foods`` second corpus from the Open Food Facts CSV export.

Public reference data (no personal values). Adds branded/regional European product
coverage that USDA FoodData Central lacks — the retrieval error tail the stratified
benchmark localizes to `regional` and `branded` foods.

Usage:
    uv run python scripts/build_openfoodfacts.py [--db DB] [--csv PATH] [--countries hu,de,at]
"""

from __future__ import annotations

import argparse
import os
import time

from syncology import db
from syncology.ingest import openfoodfacts as off

_NAMES = {"hu": "hungary", "de": "germany", "at": "austria"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument(
        "--csv",
        default=os.path.join(
            os.environ.get("SYNCOLOGY_DATA_DIR", "data"),
            "raw/public/openfoodfacts/products.csv.gz",
        ),
    )
    ap.add_argument("--countries", default="hu,de,at",
                    help="comma codes among hu,de,at (or full country slugs)")
    args = ap.parse_args()
    countries = tuple(_NAMES.get(c.strip(), c.strip()) for c in args.countries.split(","))

    con = db.connect(args.db)
    t0 = time.perf_counter()
    n = off.build(con, args.csv, countries)
    dt = time.perf_counter() - t0

    print("=" * 60)
    print("OFF_FOODS (Open Food Facts second corpus)")
    print("=" * 60)
    print(f"countries: {countries}")
    print(f"off_foods rows: {n:,}   (built in {dt:.0f}s)")
    with_macros = con.execute(
        "SELECT count(*) FROM off_foods WHERE energy_kcal IS NOT NULL"
    ).fetchone()[0]
    with_brand = con.execute(
        "SELECT count(*) FROM off_foods WHERE brands IS NOT NULL"
    ).fetchone()[0]
    print(f"  with macros: {with_macros:,} ({with_macros / (n or 1):.0%})")
    print(f"  with brand:  {with_brand:,} ({with_brand / (n or 1):.0%})")
    print("=" * 60)
    con.close()


if __name__ == "__main__":
    main()
