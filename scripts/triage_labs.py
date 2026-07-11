"""Triage lab PDFs: text-extractable vs scanned-image, without an LLM or API.

For each PDF it reports only *structural metadata* — page count, extractable
text density, and embedded-image count — never the extracted text itself (which
contains health values). The classification decides the extraction route:

- ``text``    — enough embedded text to parse with pdfplumber directly.
- ``scanned`` — little/no text, image-backed → needs vision (OCR / VLM).
- ``mixed``   — some pages text, some image-only.

Usage:
    uv run python scripts/triage_labs.py [--dir DIR] [--json OUT.json]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pdfplumber

# A page with fewer than this many extractable characters is treated as
# image-only (a scanned page yields ~0 chars; a real text page yields hundreds).
_TEXT_PAGE_MIN_CHARS = 60


def _classify(page_chars: list[int]) -> str:
    text_pages = sum(1 for c in page_chars if c >= _TEXT_PAGE_MIN_CHARS)
    if text_pages == 0:
        return "scanned"
    if text_pages == len(page_chars):
        return "text"
    return "mixed"


def triage_file(path: Path) -> dict:
    page_chars: list[int] = []
    n_images = 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            page_chars.append(len(text.strip()))
            n_images += len(page.images)
    total_chars = sum(page_chars)
    n_pages = len(page_chars) or 1
    return {
        "file": path.name,
        "kb": round(path.stat().st_size / 1024),
        "pages": len(page_chars),
        "total_chars": total_chars,
        "chars_per_page": total_chars // n_pages,
        "images": n_images,
        "route": _classify(page_chars),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dir",
        default=os.path.join(os.environ.get("SYNCOLOGY_DATA_DIR", "data"), "raw/personal/lab"),
        help="directory of lab PDFs",
    )
    ap.add_argument("--json", help="optional path to write the manifest (metadata only)")
    args = ap.parse_args()

    pdfs = sorted(Path(args.dir).glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDFs in {args.dir}")

    rows = []
    for p in pdfs:
        try:
            rows.append(triage_file(p))
        except Exception as e:  # noqa: BLE001 — report and continue
            rows.append({"file": p.name, "kb": round(p.stat().st_size / 1024),
                         "pages": 0, "total_chars": 0, "chars_per_page": 0,
                         "images": 0, "route": f"ERROR:{type(e).__name__}"})

    print("=" * 74)
    print("LAB PDF TRIAGE  (metadata only — no extracted values shown)")
    print("=" * 74)
    print(f"{'file':<22}{'KB':>6}{'pages':>7}{'chars':>9}{'ch/pg':>8}{'imgs':>6}  route")
    for r in rows:
        print(f"{r['file']:<22}{r['kb']:>6}{r['pages']:>7}{r['total_chars']:>9}"
              f"{r['chars_per_page']:>8}{r['images']:>6}  {r['route']}")

    routes: dict[str, int] = {}
    for r in rows:
        routes[r["route"]] = routes.get(r["route"], 0) + 1
    print("-" * 74)
    print("routes:", ", ".join(f"{k}={v}" for k, v in sorted(routes.items())))
    print("=" * 74)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"manifest -> {args.json}")


if __name__ == "__main__":
    main()
