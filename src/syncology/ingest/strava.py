"""Strava CSV export → discrete activity events.

The export is Spanish-locale (``Fecha de la actividad``, ``Tipo de actividad``,
dates like ``23 jun 2026, 6:14:26``). This module parses the core per-activity
columns into normalized dicts; ``activities.py`` loads them into the unified
``activities`` table. Only structural fields are handled here — no health values
beyond the activity's own metrics (duration, distance, elevation, speed).
"""

from __future__ import annotations

import csv
import re
import unicodedata
from datetime import datetime
from pathlib import Path

# Spanish activity type -> canonical (keys are accent-stripped + lowercased).
TYPE_MAP = {
    "carrera": "run",
    "bicicleta": "ride",
    "senderismo": "hike",
    "caminata": "walk",
    "esqui alpino": "ski",
    "patinaje sobre hielo": "ice_skate",
    "patinaje en linea": "inline_skate",
    "escalada": "climb",
    "kayak": "kayak",
    "natacion": "swim",
}

_ES_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}

# Core column indices in the Strava export (there are ~80 columns; these are the
# per-activity summary fields we keep).
_COL = {
    "id": 0, "date": 1, "name": 2, "type": 3,
    "elapsed_s": 15, "moving_s": 16, "distance_m": 17,
    "max_speed": 18, "avg_speed": 19, "elevation_gain_m": 20,
}

_DATE_RE = re.compile(r"(\d{1,2})\s+([a-z]{3})[a-z.]*\s+(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def parse_date(s: str) -> datetime:
    """Parse a Spanish Strava timestamp, e.g. '23 jun 2026, 6:14:26'."""
    m = _DATE_RE.search(_strip_accents(s).lower())
    if not m:
        raise ValueError(f"unrecognized Strava date: {s!r}")
    d, mon, y, hh, mm, ss = m.groups()
    return datetime(int(y), _ES_MONTHS[mon], int(d), int(hh), int(mm), int(ss))


def _fnum(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse(path: str | Path) -> list[dict]:
    """Return normalized activity dicts from a Strava export CSV."""
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if len(row) <= _COL["elevation_gain_m"]:
                continue
            raw_type = _strip_accents(row[_COL["type"]]).lower().strip()
            dist_m = _fnum(row[_COL["distance_m"]])
            out.append(
                {
                    "activity_id": f"strava-{row[_COL['id']]}",
                    "source": "strava",
                    "activity_type": TYPE_MAP.get(raw_type, raw_type or "other"),
                    "name": row[_COL["name"]] or None,
                    "start_ts": parse_date(row[_COL["date"]]),
                    "duration_s": _fnum(row[_COL["elapsed_s"]]),
                    "moving_s": _fnum(row[_COL["moving_s"]]),
                    "distance_km": dist_m / 1000 if dist_m is not None else None,
                    "elevation_gain_m": _fnum(row[_COL["elevation_gain_m"]]),
                    "avg_speed": _fnum(row[_COL["avg_speed"]]),
                    "max_speed": _fnum(row[_COL["max_speed"]]),
                }
            )
    return out
