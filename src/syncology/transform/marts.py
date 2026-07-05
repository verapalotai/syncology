"""Daily marts + conservative cycle-phase inference.

Builds three day-grain marts over the warehouse:

- ``daily_activity``  — steps, distance, energy, flights, and Activity-ring
  summary per day.
- ``daily_nutrition`` — macro totals per day from the ``Dietary*`` metrics.
- ``cycle_days``      — raw cycle signals per day (BBT, flow, mucus, LH, IMB).

and two derived tables:

- ``cycle_phases``    — per day: clinical phase, fertility zone, fertile-window
  flag, and cycle day, inferred from ``cycle_days``.
- ``cycle_summary``   — one row per menses-to-menses cycle with its length,
  luteal length, ovulation/peak/confirmation markers, a short-luteal flag, and
  z-scores/flags against population norms, so out-of-norm cycles (long / short
  luteal — common in PMOS) surface explicitly.

**Day grain.** Measurements are stored as UTC instants; a "day" here is the
*local* calendar date (default ``Europe/Budapest``), so a 23:00 reading lands on
the right day rather than rolling into the next UTC day.

**Cycle-phase inference — the sympto-thermal method (STM).** Implements the
established STM fertility-awareness rules, cross-checking the two primary
biomarkers. It stays conservative with an explicit ``unknown``: PMOS cycles are
frequently long / irregular / anovulatory, so structure is never fabricated.

*Temperature is the only sign that confirms ovulation occurred.* Coverline rule:
after 6 low readings, look for 3 readings above the highest of those 6 (the
coverline); the 3rd must sit at least ``shift_c`` (0.2 °C, the standard STM
value) above the coverline. Exception 1: if the 3rd isn't a full 0.2 above, a 4th reading
above the line confirms instead. Exception 2 (conservative): if any reading in
the run falls back to/below the line, that run is void. The last low day before
the rise is taken as ovulation.

*Mucus predicts; it does not confirm.* The peak day (``csúcsnap``) is the last
day of peak-type mucus (watery / egg-white); mucus "confirms" at peak + 3 days.
Before ovulation, *any* mucus counts as fertile (the change point = first mucus).

*Cross-check:* whichever sign confirms **later** wins — confirmation day =
``max(temp-confirm, peak + 3)`` — but a temperature shift is required at all.

Outputs per day:

- clinical ``phase`` — ``menstruation`` / ``follicular`` / ``ovulation`` /
  ``luteal`` / ``unknown`` (the last for any unconfirmed / anovulatory stretch).
- ``fertility_zone`` — the STM zones: ``infertile_pre`` (relative-infertile,
  menses → first mucus), ``fertile`` (first mucus → confirmation), or
  ``infertile_post`` (absolute-infertile, after confirmation); ``unknown`` when
  signals are insufficient.
- ``fertile_window`` — boolean shorthand for ``fertility_zone == 'fertile'``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

DEFAULT_TZ = "Europe/Budapest"

# Sympto-thermal thermal-shift detection. The coverline is the highest of the
# preceding LOW_WINDOW readings; a shift needs HIGH_RUN readings above it, the
# last at least ``shift_c`` above (or a 4th above the line — Exception 1).
# Operates on days that actually have a BBT reading (gaps are common).
_LOW_WINDOW = 6
_HIGH_RUN = 3

# 3rd-high margin above the coverline, in °C. The standard STM value is 0.2 —
# note this is NOT the old flat "all highs above max+X" threshold: here the
# coverline has no offset and only the 3rd high must clear it by this margin, so
# it detects more than an all-three rule at the same number would. Tunable via
# the sweep in scripts/sweep_ovulation_threshold.py; see docs/schema.md.
DEFAULT_SHIFT_C = 0.2

# Cervical-mucus ordinals (see transform/category_values.py): >= _PEAK_MUCUS is
# peak-type (watery / egg-white); >= _ANY_MUCUS is any mucus (the change point).
_PEAK_MUCUS = 4
_ANY_MUCUS = 1
_MUCUS_CONFIRM_DAYS = 3  # peak day + 3 = mucus confirmation (CS+3)

# Fallback fertile-window start before ovulation when no mucus is logged
# (≈ sperm survival, in days). Clinical short-luteal threshold (low progesterone).
_FERTILE_LOOKBACK = 5
_SHORT_LUTEAL_DAYS = 10

# Physiological max luteal length (days). The luteal phase runs to ~16 days at
# most, so a detected shift implying a longer luteal is almost certainly a
# false-early shift (common in long PMOS cycles); it is skipped and scanning
# continues — see _detect_temp_shift's guard.
_MAX_LUTEAL_DAYS = 16

# Population reference norms for anomaly flagging: menstrual cycle and luteal
# phase lengths (days) from Fehring et al. (2012) menstrual-cycle-phase data —
# the standard prospective norms (n counts below are the study's per-phase
# samples). Used only to compute z-scores; a |z| beyond FLAG_SIGMA is flagged as
# out-of-norm — expected often for PMOS, which is exactly the signal we want.
POP_NORMS = {
    "cycle_length": {"mean": 29.30, "std": 3.89, "n": 1665},
    "luteal_length": {"mean": 13.27, "std": 2.67, "n": 1514},
}
FLAG_SIGMA = 2.0


@dataclass
class MartStats:
    """Row counts + phase distribution from a marts build, for reporting."""

    daily_activity_rows: int = 0
    daily_nutrition_rows: int = 0
    cycle_days_rows: int = 0
    phase_counts: dict[str, int] = field(default_factory=dict)
    n_cycles: int = 0
    n_ovulatory_cycles: int = 0
    n_flagged_cycles: int = 0
    n_suspect_cycles: int = 0
    shift_c: float = DEFAULT_SHIFT_C


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


@dataclass
class Cycle:
    """One menses-to-menses cycle with its inferred STM markers and lengths."""

    number: int  # 1-based ordinal in observed order
    start: date  # menses onset (cycle day 1)
    next_start: date | None
    ovulation: date | None  # last low day before the temp rise (temp estimate)
    temp_confirm: date | None  # day the temperature run confirms (3rd/4th high)
    peak_day: date | None  # last peak-type mucus day (csúcsnap)
    confirmation: date | None  # cross-checked: max(temp_confirm, peak + 3)
    length_days: int | None  # start -> next menses onset (None if censored/last)
    follicular_days: int | None  # start -> ovulation, inclusive
    luteal_days: int | None  # ovulation -> next menses onset
    short_luteal: bool  # luteal <= _SHORT_LUTEAL_DAYS (low-progesterone flag)
    suspect_ovulation: bool  # a shift was seen but skipped as a false-early shift


def _z(value: float, norm: dict) -> float:
    return (value - norm["mean"]) / norm["std"]


def _detect_temp_shift(
    series: list[tuple[date, float]],
    shift_c: float = DEFAULT_SHIFT_C,
    cycle_end: date | None = None,
    max_luteal: int | None = None,
) -> tuple[date | None, date | None, bool]:
    """Return ``(ovulation_day, confirmation_day, suspect)`` from a BBT series.

    Implements the STM coverline rule (see the module docstring): coverline =
    highest of the 6 low readings; the next 3 readings must all be above it, and
    the 3rd at least ``shift_c`` above (Exception 1: else a 4th above the line
    confirms). Exception 2: a reading falling back to/below the line voids that
    run (conservative). ``series`` is ``(day, bbt)`` in date order over days that
    have a reading. Ovulation is the last low day before the rise; the
    confirmation day is the 3rd (or 4th) high.

    Physiological guard: when ``cycle_end`` and ``max_luteal`` are given, a shift
    whose implied luteal (``cycle_end - ovulation``) exceeds ``max_luteal`` is a
    false-early shift — it is skipped and scanning continues for a plausible later
    one. ``suspect`` is True if at least one shift was skipped by this guard.
    Returns ``(None, None, suspect)`` if no acceptable shift is found.
    """
    suspect = False
    n = len(series)
    for i in range(_LOW_WINDOW, n - _HIGH_RUN + 1):
        coverline = max(b for _, b in series[i - _LOW_WINDOW:i])
        highs = series[i:]
        if any(highs[k][1] <= coverline for k in range(_HIGH_RUN)):
            continue  # Exception 2: a non-elevated reading voids this run
        ov = series[i - 1][0]  # last low day before the rise
        if highs[_HIGH_RUN - 1][1] >= coverline + shift_c:
            confirm = highs[_HIGH_RUN - 1][0]
        elif len(highs) > _HIGH_RUN and highs[_HIGH_RUN][1] > coverline:
            confirm = highs[_HIGH_RUN][0]  # Exception 1: a 4th above the line
        else:
            continue
        if (
            cycle_end is not None
            and max_luteal is not None
            and (cycle_end - ov).days > max_luteal
        ):
            suspect = True  # false-early shift: implausibly long luteal -> skip
            continue
        return ov, confirm, suspect
    return None, None, suspect


def analyze_cycles(
    rows: list[dict], shift_c: float = DEFAULT_SHIFT_C
) -> tuple[list[dict], list[Cycle]]:
    """Infer per-day phases/zones and per-cycle summaries from ``cycle_days`` rows.

    ``rows`` must be sorted by ``day``. Returns ``(per_day, cycles)`` where
    ``per_day`` is dicts of ``{day, phase, fertility_zone, fertile_window,
    cycle_day}`` and ``cycles`` is the list of :class:`Cycle`. See the module
    docstring for the sympto-thermal policy.
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
    zone = {d: "unknown" for d in days}
    cycle_day = {d: None for d in days}
    cycles: list[Cycle] = []

    # Bound each cycle by the next cycle start.
    bounds = list(zip(cycle_starts, cycle_starts[1:] + [None]))
    for number, (start, nxt) in enumerate(bounds, start=1):
        window = [d for d in days if d >= start and (nxt is None or d < nxt)]
        for d in window:
            cycle_day[d] = (d - start).days + 1
            if d in menses:
                phase[d] = "menstruation"

        # Temperature — the only sign that confirms ovulation occurred. The
        # guard rejects a false-early shift implying an implausibly long luteal.
        series = [(d, bbt[d]) for d in window if d in bbt]
        ov, temp_confirm, suspect = _detect_temp_shift(
            series, shift_c, cycle_end=nxt, max_luteal=_MAX_LUTEAL_DAYS
        )

        # Mucus — change point (first any-mucus) and peak day (last peak-type).
        mucus_days = [d for d in window if mucus[d] >= _ANY_MUCUS]
        change_point = mucus_days[0] if mucus_days else None
        peak_days = [d for d in window if mucus[d] >= _PEAK_MUCUS]
        peak_day = peak_days[-1] if peak_days else None

        # Cross-check: confirmation only with a temperature shift; the later of
        # the two signs wins.
        confirmation = None
        if temp_confirm is not None:
            confirmation = temp_confirm
            if peak_day is not None:
                mucus_confirm = peak_day + timedelta(days=_MUCUS_CONFIRM_DAYS)
                confirmation = max(temp_confirm, mucus_confirm)

        # Clinical phases around a confirmed ovulation (else stays "unknown").
        if ov is not None:
            for d in window:
                if d in menses:
                    continue
                if d < ov:
                    phase[d] = "follicular"
                elif d == ov:
                    phase[d] = "ovulation"
                else:
                    phase[d] = "luteal"

        # Fertility zones. The fertile window opens at first mucus (change point),
        # or falls back to ~sperm-survival before a temp-detected ovulation.
        fertile_start = change_point
        if fertile_start is None and ov is not None:
            fertile_start = ov - timedelta(days=_FERTILE_LOOKBACK)
        for d in window:
            if fertile_start is None:
                continue  # no usable signal -> stays "unknown"
            if d < fertile_start:
                zone[d] = "infertile_pre"
            elif confirmation is not None and d > confirmation:
                zone[d] = "infertile_post"
            else:
                zone[d] = "fertile"  # open-ended if ovulation never confirmed

        length = (nxt - start).days if nxt is not None else None
        follicular = (ov - start).days + 1 if ov is not None else None
        luteal = (nxt - ov).days if (ov is not None and nxt is not None) else None
        short_luteal = luteal is not None and luteal <= _SHORT_LUTEAL_DAYS
        cycles.append(
            Cycle(number, start, nxt, ov, temp_confirm, peak_day, confirmation,
                  length, follicular, luteal, short_luteal, suspect)
        )

    # Peak mucus / LH surge are fertile signals in their own right — but never
    # override the post-confirmation absolute-infertile zone (a pre-menstrual
    # "false peak" does not reopen fertility once ovulation is confirmed).
    for d in days:
        if (mucus[d] >= _PEAK_MUCUS or lh[d]) and zone[d] != "infertile_post":
            zone[d] = "fertile"

    per_day = [
        {
            "day": d,
            "phase": phase[d],
            "fertility_zone": zone[d],
            "fertile_window": zone[d] == "fertile",
            "cycle_day": cycle_day[d],
        }
        for d in days
    ]
    return per_day, cycles


def write_cycle_phases(con: duckdb.DuckDBPyConnection, per_day: list[dict]) -> dict[str, int]:
    """Materialize the ``cycle_phases`` table; return the phase-count map."""
    con.execute("DROP TABLE IF EXISTS cycle_phases")
    con.execute(
        """
        CREATE TABLE cycle_phases (
            day            DATE PRIMARY KEY,
            phase          VARCHAR NOT NULL,
            fertility_zone VARCHAR NOT NULL,
            fertile_window BOOLEAN NOT NULL,
            cycle_day      INTEGER
        )
        """
    )
    if per_day:
        con.executemany(
            "INSERT INTO cycle_phases VALUES (?, ?, ?, ?, ?)",
            [
                (p["day"], p["phase"], p["fertility_zone"], p["fertile_window"], p["cycle_day"])
                for p in per_day
            ],
        )
    return dict(con.execute("SELECT phase, count(*) FROM cycle_phases GROUP BY phase").fetchall())


def write_cycle_summary(
    con: duckdb.DuckDBPyConnection, cycles: list[Cycle]
) -> tuple[int, int, int, int]:
    """Materialize ``cycle_summary`` with population-norm z-scores + flags.

    Returns ``(n_cycles, n_ovulatory, n_flagged, n_suspect)``. Cycle/luteal
    lengths are z-scored against :data:`POP_NORMS`; a length beyond
    :data:`FLAG_SIGMA` sigma is flagged. Flags are NULL where the length is
    unknown (censored last cycle, or no detected ovulation).
    """
    con.execute("DROP TABLE IF EXISTS cycle_summary")
    con.execute(
        """
        CREATE TABLE cycle_summary (
            cycle_number       INTEGER PRIMARY KEY,
            cycle_start        DATE NOT NULL,
            next_start         DATE,
            cycle_length_days  INTEGER,
            ovulation_day      DATE,
            temp_confirm_day   DATE,
            peak_day           DATE,
            confirmation_day   DATE,
            follicular_days    INTEGER,
            luteal_days        INTEGER,
            short_luteal       BOOLEAN NOT NULL,
            suspect_ovulation  BOOLEAN NOT NULL,
            cycle_length_z     DOUBLE,
            luteal_length_z    DOUBLE,
            cycle_length_flag  BOOLEAN,
            luteal_length_flag BOOLEAN,
            anovulatory        BOOLEAN NOT NULL
        )
        """
    )
    out = []
    for c in cycles:
        cz = _z(c.length_days, POP_NORMS["cycle_length"]) if c.length_days is not None else None
        lz = _z(c.luteal_days, POP_NORMS["luteal_length"]) if c.luteal_days is not None else None
        out.append(
            (
                c.number,
                c.start,
                c.next_start,
                c.length_days,
                c.ovulation,
                c.temp_confirm,
                c.peak_day,
                c.confirmation,
                c.follicular_days,
                c.luteal_days,
                c.short_luteal,
                c.suspect_ovulation,
                round(cz, 3) if cz is not None else None,
                round(lz, 3) if lz is not None else None,
                (abs(cz) > FLAG_SIGMA) if cz is not None else None,
                (abs(lz) > FLAG_SIGMA) if lz is not None else None,
                c.ovulation is None,
            )
        )
    if out:
        con.executemany(
            "INSERT INTO cycle_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            out,
        )
    n_ovulatory = sum(1 for c in cycles if c.ovulation is not None)
    n_suspect = sum(1 for c in cycles if c.suspect_ovulation)
    n_flagged = con.execute(
        "SELECT count(*) FROM cycle_summary WHERE cycle_length_flag OR luteal_length_flag"
    ).fetchone()[0]
    return len(cycles), n_ovulatory, n_flagged, n_suspect


def _fetch_cycle_days(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT day, bbt_c, flow_ordinal, mucus_ordinal, intermenstrual_bleeding, lh_surge
        FROM cycle_days ORDER BY day
        """
    ).fetchall()
    cols = ["day", "bbt_c", "flow_ordinal", "mucus_ordinal", "intermenstrual_bleeding", "lh_surge"]
    return [dict(zip(cols, r)) for r in rows]


def apply(
    con: duckdb.DuckDBPyConnection, tz: str = DEFAULT_TZ, shift_c: float = DEFAULT_SHIFT_C
) -> MartStats:
    """Build all daily marts, cycle phases, and the cycle summary. Idempotent."""
    stats = MartStats(shift_c=shift_c)
    build_daily_activity(con, tz)
    build_daily_nutrition(con, tz)
    build_cycle_days(con, tz)

    per_day, cycles = analyze_cycles(_fetch_cycle_days(con), shift_c)
    stats.phase_counts = write_cycle_phases(con, per_day)
    (
        stats.n_cycles,
        stats.n_ovulatory_cycles,
        stats.n_flagged_cycles,
        stats.n_suspect_cycles,
    ) = write_cycle_summary(con, cycles)

    stats.daily_activity_rows = con.execute("SELECT count(*) FROM daily_activity").fetchone()[0]
    stats.daily_nutrition_rows = con.execute("SELECT count(*) FROM daily_nutrition").fetchone()[0]
    stats.cycle_days_rows = con.execute("SELECT count(*) FROM cycle_days").fetchone()[0]
    return stats
