"""Normalize HealthKit category strings to clean labels + ordinals.

Category records store their value as a HealthKit enum string, e.g.
``HKCategoryValueCervicalMucusQualityEggWhite`` or
``HKCategoryValueVaginalBleedingMedium``. This module maps each raw enum to a
tidy ``label`` and, where the category is meaningfully ordered, an integer
``ordinal`` (rising intensity / fertility signal). The mapping is materialized as
a small, auditable ``category_value_map`` table and exposed through the
``measurements_categorized`` view, which adds ``value_label`` / ``value_ordinal``
columns to every measurement while leaving the ``measurements`` table canonical.

This is the project's first entity-resolution step, so the mapping is explicit
and inspectable rather than hidden in code branches. Values not yet present in
the export (e.g. ones an Apple Watch will add) are included so future data does
not silently fall through unmapped — and any genuinely new value is surfaced by
``unmapped_values`` rather than dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

# metric -> { raw HealthKit value : (label, ordinal) }.
# ``ordinal`` ranks meaningfully-ordered categories (menstrual-flow intensity,
# cervical-mucus fertility signal) and is None where the category is nominal or a
# bare presence marker. Both spellings of the flow enum (``VaginalBleeding*`` and
# ``MenstrualFlow*``) are mapped because Apple has used each across HealthKit
# versions; the current export uses ``VaginalBleeding*``.
CATEGORY_VALUES: dict[str, dict[str, tuple[str, int | None]]] = {
    "MenstrualFlow": {
        "HKCategoryValueVaginalBleedingUnspecified": ("unspecified", None),
        "HKCategoryValueMenstrualFlowUnspecified": ("unspecified", None),
        "HKCategoryValueVaginalBleedingNone": ("none", 0),
        "HKCategoryValueMenstrualFlowNone": ("none", 0),
        "HKCategoryValueVaginalBleedingLight": ("light", 1),
        "HKCategoryValueMenstrualFlowLight": ("light", 1),
        "HKCategoryValueVaginalBleedingMedium": ("medium", 2),
        "HKCategoryValueMenstrualFlowMedium": ("medium", 2),
        "HKCategoryValueVaginalBleedingHeavy": ("heavy", 3),
        "HKCategoryValueMenstrualFlowHeavy": ("heavy", 3),
    },
    "CervicalMucusQuality": {
        "HKCategoryValueCervicalMucusQualityDry": ("dry", 1),
        "HKCategoryValueCervicalMucusQualitySticky": ("sticky", 2),
        "HKCategoryValueCervicalMucusQualityCreamy": ("creamy", 3),
        "HKCategoryValueCervicalMucusQualityWatery": ("watery", 4),
        "HKCategoryValueCervicalMucusQualityEggWhite": ("egg_white", 5),
    },
    "OvulationTestResult": {
        # Ordinal encodes rising fertility signal; "indeterminate" is nominal.
        "HKCategoryValueOvulationTestResultNegative": ("negative", 0),
        "HKCategoryValueOvulationTestResultIndeterminate": ("indeterminate", None),
        "HKCategoryValueOvulationTestResultEstrogenSurge": ("estrogen_surge", 1),
        "HKCategoryValueOvulationTestResultLuteinizingHormoneSurge": ("lh_surge", 2),
        "HKCategoryValueOvulationTestResultPositive": ("positive", 2),
    },
    # Presence-only category: HealthKit stores HKCategoryValueNotApplicable and
    # the sample's existence is itself the signal.
    "IntermenstrualBleeding": {
        "HKCategoryValueNotApplicable": ("present", None),
    },
    # Device event, not cycle data; mapped so it is not reported as unmapped.
    "HeadphoneAudioExposureEvent": {
        "HKCategoryValueHeadphoneAudioExposureEventSevenDayLimit": ("seven_day_limit", None),
    },
}

MAP_TABLE = "category_value_map"
VIEW = "measurements_categorized"


@dataclass
class NormalizeStats:
    """Coverage of a normalization pass, for reporting and verification."""

    mappings: int = 0
    categorized_rows: int = 0
    unmapped: list[tuple[str, str, int]] = field(default_factory=list)


def _map_rows() -> list[tuple[str, str, str, int | None]]:
    return [
        (metric, raw, label, ordinal)
        for metric, values in CATEGORY_VALUES.items()
        for raw, (label, ordinal) in values.items()
    ]


def build_map_table(con: duckdb.DuckDBPyConnection) -> int:
    """(Re)create ``category_value_map`` from ``CATEGORY_VALUES``; return row count."""
    con.execute(f"DROP TABLE IF EXISTS {MAP_TABLE}")
    con.execute(
        f"""
        CREATE TABLE {MAP_TABLE} (
            metric    VARCHAR NOT NULL,
            raw_value VARCHAR NOT NULL,
            label     VARCHAR NOT NULL,
            ordinal   INTEGER,
            PRIMARY KEY (metric, raw_value)
        )
        """
    )
    rows = _map_rows()
    con.executemany(f"INSERT INTO {MAP_TABLE} VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def create_view(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the ``measurements_categorized`` view over the map table."""
    con.execute(
        f"""
        CREATE OR REPLACE VIEW {VIEW} AS
        SELECT m.*, c.label AS value_label, c.ordinal AS value_ordinal
        FROM measurements m
        LEFT JOIN {MAP_TABLE} c
          ON m.metric = c.metric AND m.value_str = c.raw_value
        """
    )


def unmapped_values(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, int]]:
    """Category values present in ``measurements`` but absent from the map."""
    return con.execute(
        f"""
        SELECT m.metric, m.value_str, count(*) AS n
        FROM measurements m
        LEFT JOIN {MAP_TABLE} c
          ON m.metric = c.metric AND m.value_str = c.raw_value
        WHERE m.value_str IS NOT NULL AND c.raw_value IS NULL
        GROUP BY m.metric, m.value_str
        ORDER BY n DESC
        """
    ).fetchall()


def apply(con: duckdb.DuckDBPyConnection) -> NormalizeStats:
    """Build the map table + categorized view and report coverage.

    Idempotent: the table is rebuilt and the view replaced on every call.
    """
    stats = NormalizeStats()
    stats.mappings = build_map_table(con)
    create_view(con)
    stats.categorized_rows = con.execute(
        f"SELECT count(*) FROM {VIEW} WHERE value_label IS NOT NULL"
    ).fetchone()[0]
    stats.unmapped = unmapped_values(con)
    return stats
