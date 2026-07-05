"""Tests for HealthKit category-value normalization (synthetic values only)."""

from __future__ import annotations

import pytest

from syncology import db
from syncology.transform import category_values as cv


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "test.duckdb")
    yield connection
    connection.close()


def _insert_category(con, metric: str, value_str: str) -> None:
    con.execute(
        "INSERT INTO measurements (row_key, metric, value_str, start_ts, source) "
        "VALUES (?, ?, ?, ?, ?)",
        [f"{metric}:{value_str}", metric, value_str, "2026-01-01 00:00:00+00", "test"],
    )


def test_labels_and_ordinals_are_ordered(con):
    _insert_category(con, "MenstrualFlow", "HKCategoryValueVaginalBleedingLight")
    _insert_category(con, "MenstrualFlow", "HKCategoryValueVaginalBleedingHeavy")
    cv.apply(con)
    got = con.execute(
        "SELECT value_label, value_ordinal FROM measurements_categorized "
        "WHERE metric = 'MenstrualFlow' ORDER BY value_ordinal"
    ).fetchall()
    assert got == [("light", 1), ("heavy", 3)]


def test_full_export_categories_all_map(con):
    # Every category value observed in the real export must be covered.
    observed = [
        ("CervicalMucusQuality", "HKCategoryValueCervicalMucusQualityEggWhite"),
        ("CervicalMucusQuality", "HKCategoryValueCervicalMucusQualityCreamy"),
        ("CervicalMucusQuality", "HKCategoryValueCervicalMucusQualityDry"),
        ("CervicalMucusQuality", "HKCategoryValueCervicalMucusQualitySticky"),
        ("MenstrualFlow", "HKCategoryValueVaginalBleedingLight"),
        ("MenstrualFlow", "HKCategoryValueVaginalBleedingMedium"),
        ("MenstrualFlow", "HKCategoryValueVaginalBleedingHeavy"),
        ("IntermenstrualBleeding", "HKCategoryValueNotApplicable"),
        ("OvulationTestResult", "HKCategoryValueOvulationTestResultLuteinizingHormoneSurge"),
        ("HeadphoneAudioExposureEvent", "HKCategoryValueHeadphoneAudioExposureEventSevenDayLimit"),
    ]
    for metric, value in observed:
        _insert_category(con, metric, value)
    stats = cv.apply(con)
    assert stats.unmapped == []
    assert stats.categorized_rows == len(observed)


def test_presence_marker(con):
    _insert_category(con, "IntermenstrualBleeding", "HKCategoryValueNotApplicable")
    cv.apply(con)
    label, ordinal = con.execute(
        "SELECT value_label, value_ordinal FROM measurements_categorized "
        "WHERE metric = 'IntermenstrualBleeding'"
    ).fetchone()
    assert label == "present"
    assert ordinal is None


def test_unknown_value_is_flagged_not_dropped(con):
    _insert_category(con, "MenstrualFlow", "HKCategoryValueBrandNewFromWatch")
    stats = cv.apply(con)
    assert ("MenstrualFlow", "HKCategoryValueBrandNewFromWatch", 1) in stats.unmapped
    # The row still exists in the view, just without a label.
    label = con.execute(
        "SELECT value_label FROM measurements_categorized "
        "WHERE value_str = 'HKCategoryValueBrandNewFromWatch'"
    ).fetchone()[0]
    assert label is None


def test_numeric_rows_are_untouched(con):
    con.execute(
        "INSERT INTO measurements (row_key, metric, value_num, start_ts, source) "
        "VALUES (?, ?, ?, ?, ?)",
        ["step:1", "StepCount", 1234.0, "2026-01-01 00:00:00+00", "test"],
    )
    cv.apply(con)
    value_num, label = con.execute(
        "SELECT value_num, value_label FROM measurements_categorized WHERE metric = 'StepCount'"
    ).fetchone()
    assert value_num == 1234.0
    assert label is None
