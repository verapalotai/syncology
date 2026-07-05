"""Daily marts + conservative cycle-phase inference.

Builds three day-grain marts over the warehouse:

- ``daily_activity``  — steps, distance, energy, flights, and Activity-ring
  summary per day.
- ``daily_nutrition`` — macro totals per day from the ``Dietary*`` metrics.
- ``cycle_days``      — raw cycle signals per day (BBT, flow, mucus, LH, IMB).

and one derived table:

- ``cycle_phases``    — a phase label per day inferred from ``cycle_days``.

**Day grain.** Measurements are stored as UTC instants; a "day" here is the
*local* calendar date (default ``Europe/Budapest``), so a 23:00 reading lands on
the right day rather than rolling into the next UTC day.

**Cycle-phase policy — conservative, with explicit ``unknown``.** PMOS cycles are
frequently long, irregular, or anovulatory, so a textbook always-assign 4-phase
model would fabricate structure that isn't there. Instead:

- ``menstruation`` — days with observed menstrual flow (light or above).
- ``ovulation``    — only when a sustained BBT thermal shift is detected (the
  sympto-thermal "3-over-6" rule); the last low-temperature day before the rise
  is taken as ovulation.
- ``follicular`` / ``luteal`` — assigned around a *detected* ovulation only.
- ``unknown``     — every stretch where ovulation cannot be confirmed
  (anovulatory, ambiguous, or simply no data). This is the honest default, not a
  failure.

A separate ``fertile_window`` flag marks peak cervical mucus (egg-white), an
LH surge, or the days immediately around a detected ovulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

DEFAULT_TZ = "Europe/Budapest"

# Sympto-thermal thermal-shift detection. The coverline is the highest of the
# preceding LOW_WINDOW readings; a shift is confirmed when HIGH_RUN consecutive
# readings sit at least SHIFT_C above it. Operates on days that actually have a
# BBT reading (gaps are common), not strictly consecutive calendar days.
_LOW_WINDOW = 6
_HIGH_RUN = 3
_SHIFT_C = 0.2

# Fertile-window span relative to a detected ovulation day (inclusive offsets).
_FERTILE_BEFORE = 5
_FERTILE_AFTER = 1


@dataclass
class MartStats:
    """Row counts + phase distribution from a marts build, for reporting."""

    daily_activity_rows: int = 0
    daily_nutrition_rows: int = 0
    cycle_days_rows: int = 0
    phase_counts: dict[str, int] = field(default_factory=dict)


def _local_day(col: str, tz: str) -> str:
    """SQL snippet casting a TIMESTAMPTZ column to its local calendar date."""
    return f"CAST({col} AT TIME ZONE '{tz}' AS DATE)"


def build_daily_activity(con: duckdb.DuckDBPyConnection, tz: str = DEFAULT_TZ) -> None:
    day = _local_day("start_ts", tz)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW daily_activity AS
        WITH agg AS (
            SELECT {day} AS day,
                sum(value_num) FILTER (WHERE metric = 'StepCount')              AS steps,
                sum(value_num) FILTER (WHERE metric = 'DistanceWalkingRunning') AS distance_km,
                sum(value_num) FILTER (WHERE metric = 'ActiveEnergyBurned')     AS active_energy_kcal,
                sum(value_num) FILTER (WHERE metric = 'BasalEnergyBurned')      AS basal_energy_kcal,
                sum(value_num) FILTER (WHERE metric = 'FlightsClimbed')         AS flights_climbed
            FROM measurements
            WHERE metric IN (
                'StepCount', 'DistanceWalkingRunning', 'ActiveEnergyBurned',
                'BasalEnergyBurned', 'FlightsClimbed'
            )
            GROUP BY 1
        )
        SELECT
            COALESCE(a.day, CAST(s.date_components AS DATE)) AS day,
            a.steps, a.distance_km, a.active_energy_kcal, a.basal_energy_kcal,
            a.flights_climbed,
            s.exercise_time AS exercise_min,
            s.stand_hours,
            s.move_time
        FROM agg a
        FULL OUTER JOIN activity_summary s
          ON a.day = CAST(s.date_components AS DATE)
        ORDER BY day
        """
    )


def build_daily_nutrition(con: duckdb.DuckDBPyConnection, tz: str = DEFAULT_TZ) -> None:
    day = _local_day("start_ts", tz)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW daily_nutrition AS
        SELECT {day} AS day,
            sum(value_num) FILTER (WHERE metric = 'DietaryEnergyConsumed') AS energy_kcal,
            sum(value_num) FILTER (WHERE metric = 'DietaryProtein')        AS protein_g,
            sum(value_num) FILTER (WHERE metric = 'DietaryCarbohydrates')  AS carbs_g,
            sum(value_num) FILTER (WHERE metric = 'DietaryFatTotal')       AS fat_g,
            sum(value_num) FILTER (WHERE metric = 'DietaryFiber')          AS fiber_g,
            sum(value_num) FILTER (WHERE metric = 'DietarySugar')          AS sugar_g,
            count(DISTINCT correlation_id)                                 AS meals_logged
        FROM measurements
        WHERE metric LIKE 'Dietary%'
        GROUP BY 1
        ORDER BY day
        """
    )


def build_cycle_days(con: duckdb.DuckDBPyConnection, tz: str = DEFAULT_TZ) -> None:
    """Raw per-day cycle signals from the categorized measurements view."""
    day = _local_day("start_ts", tz)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW cycle_days AS
        SELECT {day} AS day,
            avg(value_num) FILTER (WHERE metric = 'BasalBodyTemperature')     AS bbt_c,
            max(value_ordinal) FILTER (WHERE metric = 'MenstrualFlow')        AS flow_ordinal,
            max(value_ordinal) FILTER (WHERE metric = 'CervicalMucusQuality') AS mucus_ordinal,
            bool_or(metric = 'IntermenstrualBleeding')                        AS intermenstrual_bleeding,
            bool_or(metric = 'OvulationTestResult'
                    AND value_label IN ('lh_surge', 'positive'))             AS lh_surge
        FROM measurements_categorized
        WHERE metric IN (
            'BasalBodyTemperature', 'MenstrualFlow', 'CervicalMucusQuality',
            'IntermenstrualBleeding', 'OvulationTestResult'
        )
        GROUP BY 1
        ORDER BY day
        """
    )


def _detect_ovulation(bbt_series: list[tuple[date, float]]) -> date | None:
    """Return the inferred ovulation date within one cycle, or None.

    ``bbt_series`` is the cycle's ``(day, bbt)`` pairs in date order (only days
    that have a reading). Applies the sympto-thermal "3-over-6": the first day
    whose reading plus the next ``_HIGH_RUN - 1`` are all >= (max of the prior
    ``_LOW_WINDOW`` readings) + ``_SHIFT_C``. Ovulation is the last low day
    immediately before that run.
    """
    n = len(bbt_series)
    for i in range(_LOW_WINDOW, n - _HIGH_RUN + 1):
        coverline = max(bbt for _, bbt in bbt_series[i - _LOW_WINDOW:i]) + _SHIFT_C
        if all(bbt_series[i + k][1] >= coverline for k in range(_HIGH_RUN)):
            return bbt_series[i - 1][0]  # last low-temp day before the shift
    return None


def _infer_phases(rows: list[dict]) -> list[dict]:
    """Assign a phase label per day from ordered ``cycle_days`` rows.

    See the module docstring for the policy. ``rows`` must be sorted by ``day``.
    Returns dicts of ``{day, phase, cycle_day, fertile_window}``.
    """
    days = [r["day"] for r in rows]
    flow = {r["day"]: (r["flow_ordinal"] or 0) for r in rows}
    bbt = {r["day"]: r["bbt_c"] for r in rows if r["bbt_c"] is not None}
    mucus = {r["day"]: (r["mucus_ordinal"] or 0) for r in rows}
    lh = {r["day"]: bool(r["lh_surge"]) for r in rows}

    # Menses = flow >= 1 (light+). A cycle starts on a bleeding day whose prior
    # calendar day was not itself a bleeding day (i.e. the first day of a run).
    menses = {d for d in days if flow[d] >= 1}
    cycle_starts = [d for d in days if d in menses and (d - timedelta(days=1)) not in menses]

    phase = {d: "unknown" for d in days}
    fertile = {d: False for d in days}
    cycle_day = {d: None for d in days}

    # Bound each cycle by the next cycle start.
    bounds = list(zip(cycle_starts, cycle_starts[1:] + [None]))
    for start, nxt in bounds:
        window = [d for d in days if d >= start and (nxt is None or d < nxt)]
        for d in window:
            cycle_day[d] = (d - start).days + 1

        # Menses days first.
        for d in window:
            if d in menses:
                phase[d] = "menstruation"

        # Ovulation via thermal shift on this cycle's BBT readings.
        series = [(d, bbt[d]) for d in window if d in bbt]
        ov = _detect_ovulation(series)
        post_menses = [d for d in window if d not in menses]
        if ov is not None:
            for d in post_menses:
                if d < ov:
                    phase[d] = "follicular"
                elif d == ov:
                    phase[d] = "ovulation"
                else:
                    phase[d] = "luteal"
            for d in window:
                if 0 <= (ov - d).days <= _FERTILE_BEFORE or 0 <= (d - ov).days <= _FERTILE_AFTER:
                    fertile[d] = True
        # else: post-menses stays "unknown" (anovulatory / ambiguous).

    # Symptomatic fertile signals stand on their own, independent of BBT.
    for d in days:
        if mucus[d] >= 4 or lh[d]:
            fertile[d] = True

    return [
        {
            "day": d,
            "phase": phase[d],
            "cycle_day": cycle_day[d],
            "fertile_window": fertile[d],
        }
        for d in days
    ]


def build_cycle_phases(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Materialize ``cycle_phases`` from ``cycle_days``; return phase counts."""
    rows = con.execute(
        """
        SELECT day, bbt_c, flow_ordinal, mucus_ordinal, intermenstrual_bleeding, lh_surge
        FROM cycle_days ORDER BY day
        """
    ).fetchall()
    cols = ["day", "bbt_c", "flow_ordinal", "mucus_ordinal", "intermenstrual_bleeding", "lh_surge"]
    dict_rows = [dict(zip(cols, r)) for r in rows]
    phases = _infer_phases(dict_rows)

    con.execute("DROP TABLE IF EXISTS cycle_phases")
    con.execute(
        """
        CREATE TABLE cycle_phases (
            day            DATE PRIMARY KEY,
            phase          VARCHAR NOT NULL,
            cycle_day      INTEGER,
            fertile_window BOOLEAN NOT NULL
        )
        """
    )
    if phases:
        con.executemany(
            "INSERT INTO cycle_phases VALUES (?, ?, ?, ?)",
            [(p["day"], p["phase"], p["cycle_day"], p["fertile_window"]) for p in phases],
        )

    counts = dict(
        con.execute("SELECT phase, count(*) FROM cycle_phases GROUP BY phase").fetchall()
    )
    return counts


def apply(con: duckdb.DuckDBPyConnection, tz: str = DEFAULT_TZ) -> MartStats:
    """Build all daily marts + cycle phases. Idempotent."""
    stats = MartStats()
    build_daily_activity(con, tz)
    build_daily_nutrition(con, tz)
    build_cycle_days(con, tz)
    stats.phase_counts = build_cycle_phases(con)

    stats.daily_activity_rows = con.execute("SELECT count(*) FROM daily_activity").fetchone()[0]
    stats.daily_nutrition_rows = con.execute("SELECT count(*) FROM daily_nutrition").fetchone()[0]
    stats.cycle_days_rows = con.execute("SELECT count(*) FROM cycle_days").fetchone()[0]
    return stats
