"""Reconcile Yazio logged foods to canonical USDA foods.

Ingests the Yazio nutrition log, then matches each distinct product to a USDA
food via translate → multilingual embedding → macro-fingerprint rerank. Prints
coverage and score distribution (a small sample of matches, which are food names
— not health values).

Usage:
    uv run python scripts/reconcile_foods.py [--db DB] [--log CSV] [--no-translate]
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

from syncology import db
from syncology.ingest import yazio
from syncology.resolve import foods


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument(
        "--log",
        default=os.path.join(
            os.environ.get("SYNCOLOGY_DATA_DIR", "data"),
            "raw/personal/nutrition/nutrition_log.csv",
        ),
    )
    ap.add_argument("--no-translate", action="store_true",
                    help="skip translation (raw-embedding baseline)")
    args = ap.parse_args()

    con = db.connect(args.db)
    n_foods = yazio.build(con, args.log)
    t0 = time.perf_counter()
    n = foods.build_food_map(con, translate=not args.no_translate)
    dt = time.perf_counter() - t0

    print("=" * 60)
    print("FOOD RECONCILIATION  (Yazio → USDA)")
    print("=" * 60)
    print(f"yazio foods: {n_foods}   reconciled: {n}   in {dt:.0f}s")
    method = con.execute("SELECT any_value(method) FROM food_map").fetchone()[0]
    print(f"method: {method}")

    scores = [r[0] for r in con.execute("SELECT score FROM food_map").fetchall()]
    q = statistics.quantiles(scores, n=4)
    print(f"score: p25={q[0]:.2f} median={q[1]:.2f} p75={q[2]:.2f}")
    for lo, hi in [(0.0, 0.6), (0.6, 0.75), (0.75, 1.01)]:
        c = sum(1 for s in scores if lo <= s < hi)
        print(f"  score [{lo:.2f},{hi:.2f}): {c:>4}  ({c / n:.0%})")
    print("=" * 60)
    con.close()


if __name__ == "__main__":
    main()
