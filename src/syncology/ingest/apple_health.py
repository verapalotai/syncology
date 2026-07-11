"""Streaming Apple Health export parser.

The export XML can be hundreds of megabytes, so it is parsed with an incremental
iterator and each element is released after use — the whole document is never
held in memory. Parsing is type-agnostic: every ``Record`` element becomes a row
in the long/tidy ``measurements`` table regardless of its ``type``, so new
record types are ingested without code changes.

Loading is idempotent: each row carries a hash of its natural key. Duplicates are
dropped within a run, and the insert skips keys already in the table, so
re-running on an overlapping export does not duplicate rows. Rows are inserted in
bulk via a columnar frame rather than one statement per row.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from dateutil import parser as date_parser

import duckdb

# HealthKit type-identifier prefixes stripped to get a clean metric name, mapped
# to the kind of record they represent.
_KIND_PREFIXES = {
    "HKQuantityTypeIdentifier": "Quantity",
    "HKCategoryTypeIdentifier": "Category",
    "HKDataTypeIdentifier": "Data",
    "HKCorrelationTypeIdentifier": "Correlation",
}

# Apple Health timestamps use a fixed format, e.g. "2024-10-08 19:47:00 +0200".
# strptime against that exact format is far faster than a generic parser; a small
# cache collapses the many repeated timestamps to one parse each.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S %z"

_BATCH_SIZE = 50_000

_COLUMNS = [
    "row_key",
    "metric",
    "record_kind",
    "value_num",
    "value_str",
    "unit",
    "start_ts",
    "end_ts",
    "creation_ts",
    "source",
    "source_version",
    "correlation_id",
]

_FRAME_SCHEMA = {
    "row_key": pl.Utf8,
    "metric": pl.Utf8,
    "record_kind": pl.Utf8,
    "value_num": pl.Float64,
    "value_str": pl.Utf8,
    "unit": pl.Utf8,
    "start_ts": pl.Datetime("us", "UTC"),
    "end_ts": pl.Datetime("us", "UTC"),
    "creation_ts": pl.Datetime("us", "UTC"),
    "source": pl.Utf8,
    "source_version": pl.Utf8,
    "correlation_id": pl.Utf8,
}


@dataclass
class ParseStats:
    """Counts collected during a parse, for reporting and verification."""

    records_seen: int = 0
    rows_inserted: int = 0
    activity_summaries: int = 0
    correlations: int = 0
    correlated_rows: int = 0
    workouts: int = 0


def _clean_metric(type_str: str) -> tuple[str, str | None]:
    """Return ``(metric, record_kind)`` with the HealthKit prefix stripped."""
    for prefix, kind in _KIND_PREFIXES.items():
        if type_str.startswith(prefix):
            return type_str[len(prefix):], kind
    return type_str, None


def _make_ts_parser():
    """Return a cached timestamp parser producing UTC-aware datetimes."""
    cache: dict[str, datetime | None] = {}

    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        cached = cache.get(value, _MISSING)
        if cached is not _MISSING:
            return cached
        try:
            dt = datetime.strptime(value, _TS_FORMAT)
        except ValueError:
            dt = date_parser.parse(value)
        dt = dt.astimezone(timezone.utc)
        cache[value] = dt
        return dt

    return parse


_MISSING = object()


def _row_key(metric: str, source: str, start: str, end: str, value: str) -> str:
    natural = "\x1f".join((metric, source, start or "", end or "", value or ""))
    return hashlib.sha1(natural.encode("utf-8")).hexdigest()


def _correlation_key(el: ET.Element) -> str:
    """Stable id for a correlation, used to group its member records."""
    metric, _ = _clean_metric(el.get("type", ""))
    natural = "\x1f".join(
        (metric, el.get("sourceName", ""), el.get("startDate", ""), el.get("endDate", ""))
    )
    return "corr-" + hashlib.sha1(natural.encode("utf-8")).hexdigest()[:16]


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _workout_row(el: ET.Element, parse_ts) -> tuple:
    """A discrete workout event: type, interval, duration in seconds."""
    wtype = el.get("workoutActivityType", "").replace("HKWorkoutActivityType", "")
    duration = _num(el.get("duration"))
    unit = (el.get("durationUnit") or "").lower()
    duration_s = duration * 60 if (duration is not None and unit == "min") else duration
    start, end = el.get("startDate"), el.get("endDate")
    key = _row_key(wtype, el.get("sourceName", ""), start, end, str(duration or ""))
    return (
        key,
        wtype,
        parse_ts(start),
        parse_ts(end),
        duration_s,
        el.get("sourceName"),
        el.get("sourceVersion"),
    )


def _activity_summary_row(el: ET.Element) -> tuple:
    return (
        el.get("dateComponents"),
        _num(el.get("activeEnergyBurned")),
        _num(el.get("activeEnergyBurnedGoal")),
        el.get("activeEnergyBurnedUnit"),
        _num(el.get("appleMoveTime")),
        _num(el.get("appleMoveTimeGoal")),
        _num(el.get("appleExerciseTime")),
        _num(el.get("appleExerciseTimeGoal")),
        _num(el.get("appleStandHours")),
        _num(el.get("appleStandHoursGoal")),
    )


def parse(
    xml_path: str | Path,
    con: duckdb.DuckDBPyConnection,
    batch_size: int = _BATCH_SIZE,
) -> ParseStats:
    """Stream ``xml_path`` into the warehouse tables on ``con``.

    ``Record`` elements — including those nested inside ``Correlation`` groups —
    become ``measurements`` rows; nested records keep a reference to their
    correlation. ``ActivitySummary`` elements go to their own table.
    """
    xml_path = Path(xml_path)
    stats = ParseStats()
    parse_ts = _make_ts_parser()

    batch: list[tuple] = []
    activity_batch: list[tuple] = []
    workout_batch: list[tuple] = []
    correlation_stack: list[str] = []
    seen: set[str] = set()
    # row_key -> correlation_id for records that appear inside a Correlation.
    # Apple exports each correlated record twice: once standalone (which always
    # comes first in document order) and once nested in its Correlation. The
    # standalone copy wins dedup, so the correlation link is backfilled onto it
    # in one set-based UPDATE after the load rather than carried on the row.
    corr_of: dict[str, str] = {}

    total_before = con.execute("SELECT count(*) FROM measurements").fetchone()[0]

    def flush_measurements() -> None:
        if not batch:
            return
        frame = pl.DataFrame(batch, schema=_FRAME_SCHEMA, orient="row")  # noqa: F841
        con.execute("INSERT OR IGNORE INTO measurements SELECT * FROM frame")
        batch.clear()

    def flush_activity() -> None:
        if not activity_batch:
            return
        con.executemany(
            "INSERT OR IGNORE INTO activity_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            activity_batch,
        )
        activity_batch.clear()

    def flush_workouts() -> None:
        if not workout_batch:
            return
        con.executemany(
            "INSERT OR IGNORE INTO workouts VALUES (?, ?, ?, ?, ?, ?, ?)", workout_batch
        )
        workout_batch.clear()

    def record_row(el: ET.Element, correlation_id: str | None) -> tuple | None:
        metric, kind = _clean_metric(el.get("type", ""))
        raw_value = el.get("value")
        start = el.get("startDate")
        end = el.get("endDate")
        key = _row_key(metric, el.get("sourceName", ""), start, end, raw_value or "")
        if key in seen:
            return None
        seen.add(key)

        value_num: float | None = None
        value_str: str | None = None
        if raw_value is not None:
            try:
                value_num = float(raw_value)
            except ValueError:
                value_str = raw_value

        return (
            key,
            metric,
            kind,
            value_num,
            value_str,
            el.get("unit"),
            parse_ts(start),
            parse_ts(end),
            parse_ts(el.get("creationDate")),
            el.get("sourceName"),
            el.get("sourceVersion"),
            correlation_id,
        )

    context = ET.iterparse(str(xml_path), events=("start", "end"))
    _, root = next(context)  # consume the root start event
    depth = 1

    for event, el in context:
        if event == "start":
            depth += 1
            if el.tag == "Correlation":
                correlation_stack.append(_correlation_key(el))
            continue

        # event == "end"
        tag = el.tag
        if tag == "Record":
            stats.records_seen += 1
            correlation_id = correlation_stack[-1] if correlation_stack else None
            row = record_row(el, correlation_id)
            if row is not None:
                batch.append(row)
                if len(batch) >= batch_size:
                    flush_measurements()
            if correlation_id is not None:
                # Link this record's natural key to its correlation even when the
                # measurement row itself came from the standalone duplicate.
                metric, _ = _clean_metric(el.get("type", ""))
                corr_of[
                    _row_key(
                        metric,
                        el.get("sourceName", ""),
                        el.get("startDate"),
                        el.get("endDate"),
                        el.get("value") or "",
                    )
                ] = correlation_id
            el.clear()
        elif tag == "Correlation":
            stats.correlations += 1
            if correlation_stack:
                correlation_stack.pop()
            el.clear()
        elif tag == "ActivitySummary":
            stats.activity_summaries += 1
            activity_batch.append(_activity_summary_row(el))
            if len(activity_batch) >= batch_size:
                flush_activity()
            el.clear()
        elif tag == "Workout":
            stats.workouts += 1
            workout_batch.append(_workout_row(el, parse_ts))
            if len(workout_batch) >= batch_size:
                flush_workouts()
            el.clear()

        depth -= 1
        if depth == 1:
            # Finished a top-level element; drop accumulated children so the root
            # does not grow without bound.
            root.clear()

    flush_measurements()
    flush_activity()
    flush_workouts()

    if corr_of:
        corr_map = pl.DataFrame(  # noqa: F841 — referenced by DuckDB replacement scan
            {"row_key": list(corr_of.keys()), "correlation_id": list(corr_of.values())},
            schema={"row_key": pl.Utf8, "correlation_id": pl.Utf8},
        )
        con.execute(
            """
            UPDATE measurements
            SET correlation_id = corr_map.correlation_id
            FROM corr_map
            WHERE measurements.row_key = corr_map.row_key
            """
        )
    stats.correlated_rows = con.execute(
        "SELECT count(*) FROM measurements WHERE correlation_id IS NOT NULL"
    ).fetchone()[0]

    total_after = con.execute("SELECT count(*) FROM measurements").fetchone()[0]
    stats.rows_inserted = total_after - total_before
    return stats
