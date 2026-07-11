"""Extract structured biomarkers from lab PDFs with a local model → DuckDB.

Runs fully on-device (Ollama); no cloud call, so blood-panel values never leave
the machine. Idempotent: re-running skips rows already loaded. The terminal
report shows only per-file counts and timing — never values (the transcript
persists). Actual extracted values are written to a gitignored review file under
data/clean/ so they can be checked against the source PDFs privately.

Usage:
    uv run python scripts/extract_labs.py [--dir DIR] [--db DB] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path

from syncology import db
from syncology.ingest import labs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dir",
        default=os.path.join(os.environ.get("SYNCOLOGY_DATA_DIR", "data"), "raw/personal/lab"),
    )
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument("--engine", choices=["api", "local"], default="api",
                    help="'api' = Anthropic (reliable); 'local' = Ollama (private, needs a fitting model)")
    ap.add_argument("--reset", action="store_true", help="drop lab_results before loading")
    ap.add_argument("--limit", type=int, help="process only the first N files (for a quick check)")
    args = ap.parse_args()

    pdfs = sorted(Path(args.dir).glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        raise SystemExit(f"No PDFs in {args.dir}")

    con = db.connect(args.db)
    if args.reset:
        con.execute("DROP TABLE IF EXISTS lab_results")
    labs.ensure_schema(con)

    engine_label = (f"Anthropic API ({labs.config.BULK_MODEL})" if args.engine == "api"
                    else f"local Ollama ({labs.config.LOCAL_MODEL}) — no cloud")
    print("=" * 68)
    print(f"LAB EXTRACTION  (engine: {engine_label})")
    print("=" * 68)
    print(f"{'file':<22}{'results':>9}{'numeric':>9}{'new':>6}{'sec':>7}  status")

    total_new = tok_in = tok_out = 0
    t_start = time.perf_counter()
    for p in pdfs:
        t0 = time.perf_counter()
        try:
            text = labs.extract_text(p)
            if args.engine == "api":
                panel, ti, to = labs.extract_panel_api(text)
                tok_in += ti
                tok_out += to
            else:
                panel = labs.extract_panel(text)
            new = labs.load_panel(con, p, panel, model=(labs.config.BULK_MODEL
                                  if args.engine == "api" else labs.config.LOCAL_MODEL))
            total_new += new
            n = len(panel.results)
            numeric = sum(1 for r in panel.results if r.value_num is not None)
            print(f"{p.name:<22}{n:>9}{numeric:>9}{new:>6}{time.perf_counter() - t0:>7.0f}  ok")
        except Exception as e:  # noqa: BLE001 — report and continue
            print(f"{p.name:<22}{'—':>9}{'—':>9}{'—':>6}{time.perf_counter() - t0:>7.0f}  "
                  f"ERROR {type(e).__name__}: {str(e)[:60]}")

    total = con.execute("SELECT count(*) FROM lab_results").fetchone()[0]
    panels = con.execute("SELECT count(DISTINCT source_file) FROM lab_results").fetchone()[0]
    print("-" * 68)
    print(f"panels: {panels} | lab_results rows: {total:,} | new this run: {total_new} | "
          f"elapsed: {time.perf_counter() - t_start:.0f}s")
    if args.engine == "api" and (tok_in or tok_out):
        print(f"tokens: {tok_in:,} in / {tok_out:,} out  (see write-up for cost at model rates)")

    # Private, gitignored review file (values included) for manual validation.
    review = Path(args.db).parent / "lab_review.csv"
    rows = con.execute(
        """
        SELECT panel_date, source_file, test_name, value_num, value_str, unit,
               ref_low, ref_high, ref_text, flag
        FROM lab_results ORDER BY panel_date, source_file, test_name
        """
    ).fetchall()
    with open(review, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["panel_date", "source_file", "test_name", "value_num", "value_str",
                    "unit", "ref_low", "ref_high", "ref_text", "flag"])
        w.writerows(rows)
    print(f"review file (gitignored, values included): {review}")
    print("=" * 68)
    con.close()


if __name__ == "__main__":
    main()
