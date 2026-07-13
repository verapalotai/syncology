"""Load the DuckDB warehouse into the Kuzu knowledge graph.

Transfers via Parquet (DuckDB writes, Kuzu reads) — robust across versions and
avoids moving data through Python. Days are bucketed to the local calendar date
(``Europe/Budapest``), matching the marts. Rebuilds the graph from scratch.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from syncology.graph import ontology

TZ = "Europe/Budapest"
_LOCAL_DAY = f"CAST(start_ts AT TIME ZONE '{TZ}' AS DATE)"
_LOCAL_TS = f"CAST(start_ts AT TIME ZONE '{TZ}' AS TIMESTAMP)"

# Cycle-sign metrics modeled as Symptom observations.
_SYMPTOMS = {
    "CervicalMucusQuality": "cervical_mucus",
    "MenstrualFlow": "menstrual_flow",
    "OvulationTestResult": "lh_test",
    "IntermenstrualBleeding": "intermenstrual_bleeding",
}

# (table, SQL) for node and relationship COPYs. Relationship SELECTs put the
# FROM key first, the TO key second, then any edge properties.
_NODE_SQL: dict[str, str] = {
    "CyclePhase": "SELECT unnest(['menstruation','follicular','ovulation','luteal','unknown']) AS name",
    "Day": f"""
        SELECT d.day AS date, p.phase, p.cycle_day, p.fertility_zone
        FROM (
            SELECT day FROM cycle_days
            UNION SELECT day FROM daily_activity
            UNION SELECT day FROM daily_nutrition
            UNION SELECT {_LOCAL_DAY} AS day FROM activities
            UNION SELECT panel_date AS day FROM lab_results
        ) d
        LEFT JOIN cycle_phases p ON d.day = p.day
    """,
    "Biomarker": "SELECT key, name_en, category, unit FROM biomarker_registry",
    "LabResult": "SELECT row_key AS id, value_num AS value, unit, flag, panel_date FROM lab_results",
    "ReferenceRange": """
        SELECT key || '_ref' || CAST(
                   row_number() OVER (PARTITION BY key ORDER BY valid_from) AS VARCHAR) AS id,
               ref_low AS low, ref_high AS high, unit, n_panels, valid_from, valid_to
        FROM biomarker_reference_ranges
    """,
    "Nutrient": """
        SELECT metric AS key, replace(metric, 'Dietary', '') AS name, any_value(unit) AS unit
        FROM measurements WHERE metric LIKE 'Dietary%' GROUP BY metric
    """,
    "Food": """
        SELECT fdc_id, description, category, data_type,
               energy_kcal, protein_g, carbs_g, fat_g
        FROM foods
    """,
    "Meal": f"""
        SELECT correlation_id AS id, 'yazio' AS source, {_LOCAL_TS.replace('start_ts', 'min(start_ts)')} AS logged_ts
        FROM measurements WHERE correlation_id IS NOT NULL GROUP BY correlation_id
    """,
    "Ingredient": "SELECT key, name FROM ingredients",
    "Activity": f"SELECT activity_id AS id, activity_type, {_LOCAL_TS} AS start_ts, "
                f"duration_s, distance_km, energy_kcal FROM activities",
    "Symptom": """
        SELECT * FROM (VALUES
            ('cervical_mucus','Cervical mucus','cycle'),
            ('menstrual_flow','Menstrual flow','cycle'),
            ('lh_test','LH / ovulation test','cycle'),
            ('intermenstrual_bleeding','Intermenstrual bleeding','cycle')
        ) t(key, name, category)
    """,
}

_REL_SQL: dict[str, str] = {
    "MEASURED_AS": """
        SELECT r.row_key AS src, m.canonical_key AS dst
        FROM lab_results r JOIN biomarker_map m ON r.test_name = m.raw_name
        WHERE m.canonical_key IS NOT NULL
    """,
    "RESULT_ON": "SELECT row_key AS src, panel_date AS dst FROM lab_results",
    "REF_FOR": """
        SELECT key || '_ref' || CAST(
                   row_number() OVER (PARTITION BY key ORDER BY valid_from) AS VARCHAR) AS src,
               key AS dst
        FROM biomarker_reference_ranges
    """,
    "IN_PHASE": "SELECT day AS src, phase AS dst FROM cycle_phases WHERE phase IS NOT NULL",
    "PERFORMED_ON": f"SELECT activity_id AS src, {_LOCAL_DAY} AS dst FROM activities",
    "INTAKE_ON": f"""
        SELECT {_LOCAL_DAY} AS src, metric AS dst, sum(value_num) AS amount
        FROM measurements WHERE metric LIKE 'Dietary%' GROUP BY 1, 2
    """,
    "LOGGED_ON": f"""
        SELECT correlation_id AS src, CAST(min(start_ts) AT TIME ZONE '{TZ}' AS DATE) AS dst
        FROM measurements WHERE correlation_id IS NOT NULL GROUP BY correlation_id
    """,
    "CONTAINS": """
        SELECT correlation_id AS src, metric AS dst, sum(value_num) AS amount
        FROM measurements WHERE correlation_id IS NOT NULL AND metric LIKE 'Dietary%' GROUP BY 1, 2
    """,
    "COMPOSED_OF": """
        SELECT fdc_id AS src, ingredient_key AS dst, gram_weight
        FROM food_ingredients
    """,
    "OBSERVED_ON": f"""
        SELECT CASE metric {' '.join(f"WHEN '{m}' THEN '{s}'" for m, s in _SYMPTOMS.items())} END AS src,
               {_LOCAL_DAY} AS dst, any_value(value_label) AS value
        FROM measurements_categorized
        WHERE metric IN ({', '.join(f"'{m}'" for m in _SYMPTOMS)})
        GROUP BY 1, 2
    """,
}


def build(duckdb_path: str, kuzu_path: str) -> dict[str, int]:
    """Rebuild the Kuzu graph from the DuckDB warehouse; return node/edge counts."""
    import kuzu

    # Kuzu stores the DB as a single file (older versions used a directory);
    # remove either form plus the write-ahead log for a clean rebuild.
    p = Path(kuzu_path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink()
    wal = Path(f"{kuzu_path}.wal")
    if wal.exists():
        wal.unlink()
    tmp = Path(kuzu_path).parent / "_graph_load"
    tmp.mkdir(parents=True, exist_ok=True)

    duck = duckdb.connect(duckdb_path, read_only=True)
    kdb = kuzu.Database(kuzu_path)
    kconn = kuzu.Connection(kdb)
    ontology.create_schema(kconn)

    counts: dict[str, int] = {}
    try:
        for table, sql in {**_NODE_SQL, **_REL_SQL}.items():
            path = tmp / f"{table}.parquet"
            n = duck.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)").fetchone()
            n = n[0] if n else 0
            if n:
                kconn.execute(f'COPY {table} FROM "{path}"')
            counts[table] = n
    finally:
        duck.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return counts
