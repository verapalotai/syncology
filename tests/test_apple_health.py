"""Parser tests over a small synthetic export (no real health values)."""

from __future__ import annotations

import textwrap

import pytest

from syncology import db
from syncology.ingest import apple_health

# A synthetic export: two quantity records, one category record, an activity
# summary, and a correlation grouping two nested nutrient records. All values are
# invented for testing.
SYNTHETIC_XML = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <HealthData locale="en_US">
      <ExportDate value="2026-01-01 00:00:00 +0000"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
              value="1234" sourceName="Demo Phone" sourceVersion="1"
              creationDate="2026-01-01 08:00:00 +0000"
              startDate="2026-01-01 07:00:00 +0000"
              endDate="2026-01-01 08:00:00 +0000"/>
      <Record type="HKCategoryTypeIdentifierMenstrualFlow" value="HKCategoryValueVaginalBleedingLight"
              sourceName="Demo Tracker" sourceVersion="2"
              creationDate="2026-01-02 09:00:00 +0000"
              startDate="2026-01-02 09:00:00 +0000"
              endDate="2026-01-02 09:00:00 +0000"/>
      <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg"
              value="60.5" sourceName="Demo Phone"
              startDate="2026-01-03 06:00:00 +0000"
              endDate="2026-01-03 06:00:00 +0000"/>
      <ActivitySummary dateComponents="2026-01-01" activeEnergyBurned="400"
                       activeEnergyBurnedGoal="500" activeEnergyBurnedUnit="kcal"
                       appleExerciseTime="30" appleExerciseTimeGoal="30"
                       appleStandHours="10" appleStandHoursGoal="12"/>
      <Correlation type="HKCorrelationTypeIdentifierFood" sourceName="Demo Food"
                   startDate="2026-01-01 12:00:00 +0000"
                   endDate="2026-01-01 12:00:00 +0000">
        <Record type="HKQuantityTypeIdentifierDietaryEnergyConsumed" unit="kcal"
                value="500" sourceName="Demo Food"
                startDate="2026-01-01 12:00:00 +0000"
                endDate="2026-01-01 12:00:00 +0000"/>
        <Record type="HKQuantityTypeIdentifierDietaryProtein" unit="g"
                value="20" sourceName="Demo Food"
                startDate="2026-01-01 12:00:00 +0000"
                endDate="2026-01-01 12:00:00 +0000"/>
      </Correlation>
    </HealthData>
    """
)


@pytest.fixture
def xml_file(tmp_path):
    # Keep an accented filename to exercise non-ASCII path handling.
    path = tmp_path / "exportación.xml"
    path.write_text(SYNTHETIC_XML, encoding="utf-8")
    return path


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "test.duckdb")
    yield connection
    connection.close()


def test_parses_all_record_types(xml_file, con):
    stats = apple_health.parse(xml_file, con)
    # 3 top-level records + 2 nested nutrient records = 5.
    assert stats.records_seen == 5
    assert stats.rows_inserted == 5
    assert stats.activity_summaries == 1
    assert stats.correlations == 1

    metrics = {
        r[0] for r in con.execute("SELECT DISTINCT metric FROM measurements").fetchall()
    }
    assert metrics == {
        "StepCount",
        "MenstrualFlow",
        "BodyMass",
        "DietaryEnergyConsumed",
        "DietaryProtein",
    }


def test_quantity_vs_category_values(con, xml_file):
    apple_health.parse(xml_file, con)
    num, kind = con.execute(
        "SELECT value_num, record_kind FROM measurements WHERE metric = 'StepCount'"
    ).fetchone()
    assert num == 1234.0
    assert kind == "Quantity"

    vstr, vnum, kind = con.execute(
        "SELECT value_str, value_num, record_kind FROM measurements WHERE metric = 'MenstrualFlow'"
    ).fetchone()
    assert vstr == "HKCategoryValueVaginalBleedingLight"
    assert vnum is None
    assert kind == "Category"


def test_correlation_grouping(con, xml_file):
    apple_health.parse(xml_file, con)
    rows = con.execute(
        "SELECT metric, correlation_id FROM measurements WHERE correlation_id IS NOT NULL"
    ).fetchall()
    assert len(rows) == 2
    corr_ids = {r[1] for r in rows}
    assert len(corr_ids) == 1  # both nutrients share one correlation id

    # Non-nutrient records are not grouped.
    ungrouped = con.execute(
        "SELECT count(*) FROM measurements WHERE correlation_id IS NULL"
    ).fetchone()[0]
    assert ungrouped == 3


def test_idempotent_reingest(con, xml_file):
    apple_health.parse(xml_file, con)
    first = con.execute("SELECT count(*) FROM measurements").fetchone()[0]
    stats = apple_health.parse(xml_file, con)
    second = con.execute("SELECT count(*) FROM measurements").fetchone()[0]
    assert first == second == 5
    assert stats.rows_inserted == 0  # nothing new on the second pass
