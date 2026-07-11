"""Unified ``activities`` table — discrete exercise events from all sources.

Strava (runs / rides / hikes / …) and Apple Health workouts (Slopes ski
sessions) become rows in one ``activities`` table, each an event ``ON`` a Day.
Apple ski workouts carry only a duration in the export, so distance and energy
are enriched from the Slopes ``measurements`` rows that fall inside the session.
This is the graph's ``Activity`` node source.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from syncology.ingest import strava

ACTIVITIES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id      VARCHAR PRIMARY KEY,
    source           VARCHAR NOT NULL,
    activity_type    VARCHAR NOT NULL,
    name             VARCHAR,
    start_ts         TIMESTAMPTZ NOT NULL,
    end_ts           TIMESTAMPTZ,
    duration_s       DOUBLE,
    moving_s         DOUBLE,
    distance_km      DOUBLE,
    elevation_gain_m DOUBLE,
    avg_speed        DOUBLE,
    max_speed        DOUBLE,
    energy_kcal      DOUBLE
);
"""

_COLUMNS = (
    "activity_id", "source", "activity_type", "name", "start_ts", "end_ts",
    "duration_s", "moving_s", "distance_km", "elevation_gain_m",
    "avg_speed", "max_speed", "energy_kcal",
)

# Apple HKWorkoutActivityType (prefix already stripped) -> canonical type.
_APPLE_TYPE = {"DownhillSkiing": "ski"}


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(ACTIVITIES_SCHEMA_SQL)


def _row(d: dict) -> tuple:
    return tuple(d.get(c) for c in _COLUMNS)


def _strava_rows(csv_path: str | Path) -> list[tuple]:
    return [_row(a) for a in strava.parse(csv_path)]


def _apple_workout_rows(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Apple workouts as activities, enriched with in-window Slopes metrics."""
    workouts = con.execute(
        "SELECT row_key, workout_type, start_ts, end_ts, duration_s FROM workouts"
    ).fetchall()
    rows = []
    for row_key, wtype, start, end, dur in workouts:
        # Sum Slopes distance/energy measured during the workout window.
        dist, energy = con.execute(
            """
            SELECT
                sum(value_num) FILTER (WHERE metric = 'DistanceDownhillSnowSports'),
                sum(value_num) FILTER (WHERE metric = 'ActiveEnergyBurned')
            FROM measurements
            WHERE source = 'Slopes' AND start_ts >= ? AND start_ts <= ?
            """,
            [start, end],
        ).fetchone()
        rows.append(
            _row(
                {
                    "activity_id": f"apple-{row_key[:16]}",
                    "source": "apple_workout",
                    "activity_type": _APPLE_TYPE.get(wtype, wtype.lower()),
                    "name": wtype,
                    "start_ts": start,
                    "end_ts": end,
                    "duration_s": dur,
                    "distance_km": dist,
                    "energy_kcal": energy,
                }
            )
        )
    return rows


def build(con: duckdb.DuckDBPyConnection, strava_csv: str | Path) -> dict[str, int]:
    """(Re)build the ``activities`` table from Strava + Apple workouts."""
    con.execute("DROP TABLE IF EXISTS activities")
    ensure_schema(con)
    placeholders = ", ".join("?" * len(_COLUMNS))
    counts = {}
    for label, rows in (
        ("strava", _strava_rows(strava_csv)),
        ("apple_workout", _apple_workout_rows(con)),
    ):
        if rows:
            con.executemany(f"INSERT OR IGNORE INTO activities VALUES ({placeholders})", rows)
        counts[label] = len(rows)
    return counts
