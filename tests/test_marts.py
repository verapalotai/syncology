"""Tests for daily marts + conservative cycle-phase inference (synthetic data)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from syncology import db
from syncology.transform import category_values as cv
from syncology.transform import marts


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "test.duckdb")
    cv.apply(connection)  # cycle_days view depends on measurements_categorized
    yield connection
    connection.close()


def _measure(con, metric, *, day, num=None, string=None, corr=None):
    ts = f"{day} 12:00:00+00"
    con.execute(
        "INSERT INTO measurements (row_key, metric, value_num, value_str, start_ts, source, correlation_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [f"{metric}:{day}:{num}{string}", metric, num, string, ts, "test", corr],
    )


def test_daily_nutrition_sums_and_counts_meals(con):
    d = "2025-05-01"
    _measure(con, "DietaryEnergyConsumed", day=d, num=200, corr="m1")
    _measure(con, "DietaryEnergyConsumed", day=d, num=300, corr="m2")
    _measure(con, "DietaryProtein", day=d, num=25, corr="m1")
    marts.apply(con)
    row = con.execute(
        "SELECT energy_kcal, protein_g, meals_logged FROM daily_nutrition WHERE day = ?",
        [d],
    ).fetchone()
    assert row == (500.0, 25.0, 2)


def test_daily_activity_local_day_bucketing(con):
    # 23:30 at +02:00 is still the same local Budapest day, not the next UTC day.
    con.execute(
        "INSERT INTO measurements (row_key, metric, value_num, start_ts, source) "
        "VALUES (?, ?, ?, ?, ?)",
        ["s1", "StepCount", 1000.0, "2025-05-01 23:30:00+02", "test"],
    )
    marts.apply(con, tz="Europe/Budapest")
    days = [r[0] for r in con.execute("SELECT day FROM daily_activity").fetchall()]
    assert date(2025, 5, 1) in days


def test_thermal_shift_detects_ovulation(con):
    # A clean biphasic cycle: menses, 6 low days, then a sustained >=0.2C rise.
    start = date(2025, 5, 1)
    for i in range(3):  # 3 days of flow -> menstruation
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    low_start = start + timedelta(days=3)
    for i in range(6):  # low follicular temps
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    for i in range(3):  # thermal shift: +0.3C sustained
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=36.60)
    marts.apply(con)
    phases = dict(con.execute("SELECT day, phase FROM cycle_phases").fetchall())
    assert "menstruation" in phases.values()
    assert "ovulation" in phases.values()
    assert "follicular" in phases.values()
    assert "luteal" in phases.values()
    # Ovulation is the last low day (day index low_start+5).
    ov_day = low_start + timedelta(days=5)
    assert phases[ov_day] == "ovulation"


def test_flat_temps_stay_unknown(con):
    # Menses then flat temperatures -> no confirmable ovulation -> unknown.
    start = date(2025, 6, 1)
    for i in range(3):
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingLight")
    for i in range(12):  # flat, no shift
        _measure(con, "BasalBodyTemperature", day=start + timedelta(days=3 + i), num=36.40)
    marts.apply(con)
    phases = set(con.execute("SELECT DISTINCT phase FROM cycle_phases").fetchall())
    flat = {p[0] for p in phases}
    assert "ovulation" not in flat
    assert "unknown" in flat


def test_egg_white_mucus_flags_fertile_window(con):
    _measure(con, "CervicalMucusQuality", day="2025-07-10",
             string="HKCategoryValueCervicalMucusQualityEggWhite")
    marts.apply(con)
    fertile = con.execute(
        "SELECT fertile_window FROM cycle_phases WHERE day = ?", [date(2025, 7, 10)]
    ).fetchone()[0]
    assert fertile is True


def test_idempotent_rebuild(con):
    _measure(con, "DietaryProtein", day="2025-05-01", num=25)
    marts.apply(con)
    first = con.execute("SELECT count(*) FROM cycle_phases").fetchone()[0]
    marts.apply(con)
    second = con.execute("SELECT count(*) FROM cycle_phases").fetchone()[0]
    assert first == second
