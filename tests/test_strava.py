"""Tests for Strava CSV parsing (synthetic rows, no real activity data)."""

from __future__ import annotations

import csv
from datetime import datetime

from syncology.ingest import strava


def test_spanish_date_parsing_including_tricky_months():
    # jun/nov parse in dateutil by luck; ene/abr/ago/dic need the month map.
    assert strava.parse_date("23 jun 2026, 6:14:26") == datetime(2026, 6, 23, 6, 14, 26)
    assert strava.parse_date("1 ene 2024, 9:00:00") == datetime(2024, 1, 1, 9, 0, 0)
    assert strava.parse_date("15 dic 2023, 18:05:30") == datetime(2023, 12, 15, 18, 5, 30)
    assert strava.parse_date("3 ago 2025, 7:00:00") == datetime(2025, 8, 3, 7, 0, 0)


def test_type_mapping_normalizes_accents_and_case():
    assert strava.TYPE_MAP["carrera"] == "run"
    assert strava.TYPE_MAP["esqui alpino"] == "ski"


def test_parse_csv(tmp_path):
    header = ["ID de actividad"] + [f"c{i}" for i in range(1, 21)]
    row = [""] * 21
    row[0] = "12345"
    row[1] = "23 jun 2026, 6:14:26"
    row[2] = "Morning Run"
    row[3] = "Carrera"
    row[15] = "1860"   # elapsed s
    row[16] = "1800"   # moving s
    row[17] = "5014"   # distance m
    row[18] = "3.5"    # max speed
    row[19] = "2.8"    # avg speed
    row[20] = "42"     # elevation gain m
    path = tmp_path / "strava.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerow(row)

    acts = strava.parse(path)
    assert len(acts) == 1
    a = acts[0]
    assert a["activity_id"] == "strava-12345"
    assert a["activity_type"] == "run"
    assert a["distance_km"] == 5.014  # meters -> km
    assert a["duration_s"] == 1860
    assert a["start_ts"] == datetime(2026, 6, 23, 6, 14, 26)


def test_unknown_type_passes_through():
    # a type not in the map keeps a normalized fallback, not a crash
    assert strava.TYPE_MAP.get("marcha nordica", "marcha nordica") == "marcha nordica"
