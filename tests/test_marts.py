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


def _biphasic_cycle(con, start: date, *, cycle_len: int, high=36.60):
    """Insert one clean biphasic cycle starting at `start`, next menses at cycle_len."""
    for i in range(3):  # menses
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    low_start = start + timedelta(days=3)
    for i in range(6):  # low follicular
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    for i in range(3):  # thermal shift
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=high)
    # next cycle's menses onset defines this cycle's length
    _measure(con, "MenstrualFlow", day=start + timedelta(days=cycle_len),
             string="HKCategoryValueVaginalBleedingMedium")


def test_cycle_summary_lengths_and_ovulation(con):
    start = date(2025, 5, 1)
    _biphasic_cycle(con, start, cycle_len=28)
    marts.apply(con)
    row = con.execute(
        "SELECT cycle_length_days, ovulation_day, follicular_days, luteal_days, anovulatory "
        "FROM cycle_summary WHERE cycle_number = 1"
    ).fetchone()
    length, ov, follic, luteal, anov = row
    assert length == 28
    assert ov == start + timedelta(days=8)  # last low day (low_start + 5)
    assert follic == 9   # start..ovulation inclusive
    assert luteal == 20  # ovulation..next menses onset
    assert anov is False


def test_cycle_summary_flags_long_cycle(con):
    # A 45-day cycle is well beyond population norm (~29+/-3.9) -> flagged.
    _biphasic_cycle(con, date(2025, 5, 1), cycle_len=45)
    marts.apply(con)
    z, flag = con.execute(
        "SELECT cycle_length_z, cycle_length_flag FROM cycle_summary WHERE cycle_number = 1"
    ).fetchone()
    assert z > marts.FLAG_SIGMA
    assert flag is True


def test_anovulatory_cycle_has_null_luteal(con):
    start = date(2025, 6, 1)
    for i in range(3):
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingLight")
    for i in range(12):  # flat temps -> no shift
        _measure(con, "BasalBodyTemperature", day=start + timedelta(days=3 + i), num=36.40)
    _measure(con, "MenstrualFlow", day=start + timedelta(days=30),
             string="HKCategoryValueVaginalBleedingLight")
    marts.apply(con)
    ov, luteal, lz, anov = con.execute(
        "SELECT ovulation_day, luteal_days, luteal_length_z, anovulatory "
        "FROM cycle_summary WHERE cycle_number = 1"
    ).fetchone()
    assert ov is None
    assert luteal is None
    assert lz is None
    assert anov is True


def test_threshold_changes_detection(con):
    # A shift of ~0.15C is detected at 0.10 but not at 0.20 (no 4th temp).
    start = date(2025, 7, 1)
    for i in range(3):
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    low_start = start + timedelta(days=3)
    for i in range(6):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    for i in range(3):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=36.45)
    sensitive = marts.apply(con, shift_c=0.10)
    assert sensitive.n_ovulatory_cycles == 1
    conservative = marts.apply(con, shift_c=0.20)
    assert conservative.n_ovulatory_cycles == 0


def test_exception1_fourth_temp_confirms(con):
    # 3rd high is above the line but not by 0.2; a 4th above the line confirms.
    start = date(2025, 8, 1)
    for i in range(3):
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    low_start = start + timedelta(days=3)
    for i in range(6):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    # coverline = 36.30; three highs at +0.15 (not +0.2), then a 4th above line
    for i, t in enumerate([36.45, 36.45, 36.45, 36.42]):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=t)
    stats = marts.apply(con, shift_c=0.20)
    assert stats.n_ovulatory_cycles == 1
    confirm = con.execute(
        "SELECT temp_confirm_day FROM cycle_summary WHERE cycle_number = 1"
    ).fetchone()[0]
    assert confirm == low_start + timedelta(days=9)  # the 4th high


def test_exception2_dip_below_line_voids_run(con):
    # A single reading dipping to the coverline voids the only candidate run.
    start = date(2025, 9, 1)
    for i in range(3):
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    low_start = start + timedelta(days=3)
    for i in range(6):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    for i, t in enumerate([36.60, 36.28, 36.60]):  # middle reading dips below
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=t)
    stats = marts.apply(con, shift_c=0.20)
    assert stats.n_ovulatory_cycles == 0


def _cycle_with_mucus_and_shift(con, start: date):
    for i in range(3):  # menses
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    _measure(con, "CervicalMucusQuality", day=start + timedelta(days=5),  # change point
             string="HKCategoryValueCervicalMucusQualityCreamy")
    low_start = start + timedelta(days=3)
    for i in range(6):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    for i in range(3):  # temp confirms at start+11
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=36.60)
    _measure(con, "CervicalMucusQuality", day=start + timedelta(days=12),  # peak, later than temp
             string="HKCategoryValueCervicalMucusQualityEggWhite")
    _measure(con, "MenstrualFlow", day=start + timedelta(days=30),  # next cycle
             string="HKCategoryValueVaginalBleedingMedium")


def test_cross_check_confirmation_takes_later_sign(con):
    start = date(2025, 4, 1)
    _cycle_with_mucus_and_shift(con, start)
    marts.apply(con)
    temp_c, peak, confirm = con.execute(
        "SELECT temp_confirm_day, peak_day, confirmation_day FROM cycle_summary WHERE cycle_number = 1"
    ).fetchone()
    assert temp_c == start + timedelta(days=11)
    assert peak == start + timedelta(days=12)
    assert confirm == start + timedelta(days=15)  # peak + 3, the later sign


def test_fertility_zones(con):
    start = date(2025, 4, 1)
    _cycle_with_mucus_and_shift(con, start)
    # a luteal reading well after confirmation (start+15), so the day has a row
    _measure(con, "BasalBodyTemperature", day=start + timedelta(days=20), num=36.60)
    marts.apply(con)
    zone = dict(con.execute("SELECT day, fertility_zone FROM cycle_phases").fetchall())
    assert zone[start] == "infertile_pre"                       # menses, before mucus
    assert zone[start + timedelta(days=6)] == "fertile"          # after change point
    assert zone[start + timedelta(days=20)] == "infertile_post"  # after confirmation


def test_short_luteal_flag(con):
    start = date(2025, 10, 1)
    for i in range(3):
        _measure(con, "MenstrualFlow", day=start + timedelta(days=i),
                 string="HKCategoryValueVaginalBleedingMedium")
    low_start = start + timedelta(days=3)
    for i in range(6):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=i), num=36.30)
    for i in range(3):
        _measure(con, "BasalBodyTemperature", day=low_start + timedelta(days=6 + i), num=36.60)
    # ovulation at start+8; next menses only 8 days later -> short luteal
    _measure(con, "MenstrualFlow", day=start + timedelta(days=16),
             string="HKCategoryValueVaginalBleedingMedium")
    marts.apply(con)
    luteal, short = con.execute(
        "SELECT luteal_days, short_luteal FROM cycle_summary WHERE cycle_number = 1"
    ).fetchone()
    assert luteal == 8
    assert short is True
