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

# Canonical nutrient vocabulary — (USDA foods column, key, display name, unit, category).
# Both the logged Yazio macros and the USDA per-100g profiles map onto these keys,
# so one Nutrient node is shared by CONTAINS / INTAKE_ON / HAS_NUTRIENT. The category
# splits nutrients into energy | macro | micro.
NUTRIENT_COLS = (
    ("energy_kcal", "energy", "Energy", "kcal", "energy"),
    ("protein_g", "protein", "Protein", "g", "macro"),
    ("carbs_g", "carbohydrate", "Carbohydrate", "g", "macro"),
    ("fat_g", "fat", "Fat", "g", "macro"),
    ("saturated_fat_g", "saturated_fat", "Saturated fat", "g", "macro"),
    ("fiber_g", "fiber", "Fiber", "g", "macro"),
    ("sugars_g", "sugars", "Sugars", "g", "macro"),
    ("cholesterol_mg", "cholesterol", "Cholesterol", "mg", "micro"),
    ("sodium_mg", "sodium", "Sodium", "mg", "micro"),
    ("potassium_mg", "potassium", "Potassium", "mg", "micro"),
    ("calcium_mg", "calcium", "Calcium", "mg", "micro"),
    ("iron_mg", "iron", "Iron", "mg", "micro"),
    ("magnesium_mg", "magnesium", "Magnesium", "mg", "micro"),
    ("vitamin_d_ug", "vitamin_d", "Vitamin D", "ug", "micro"),
    ("vitamin_b12_ug", "vitamin_b12", "Vitamin B12", "ug", "micro"),
    ("folate_ug", "folate", "Folate", "ug", "micro"),
)

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
            UNION SELECT log_date AS day FROM yazio_log
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
    "Nutrient": "SELECT * FROM (VALUES "
                + ", ".join(f"('{k}', '{name}', '{u}', '{cat}')"
                            for _c, k, name, u, cat in NUTRIENT_COLS)
                + ") t(key, name, unit, category)",
    "Food": """
        SELECT fdc_id, description, category, data_type,
               energy_kcal, protein_g, carbs_g, fat_g
        FROM foods
    """,
    # A Meal is a Yazio eating occasion: a (date, meal type) with its logged macros.
    "Meal": """
        SELECT log_date || '|' || meal AS id, 'yazio' AS source,
               CAST(min(log_time) AS TIMESTAMP) AS logged_ts, meal AS meal_type,
               sum(kcal_total) AS kcal, sum(protein_total) AS protein_g,
               sum(fat_total) AS fat_g, sum(carbs_total) AS carbs_g
        FROM yazio_log GROUP BY log_date, meal
    """,
    "Ingredient": "SELECT key, name FROM ingredients",
    "Activity": f"SELECT activity_id AS id, activity_type, {_LOCAL_TS} AS start_ts, "
                f"duration_s, distance_km, energy_kcal FROM activities",
    "Symptom": """
        SELECT * FROM (VALUES
            ('bbt','Basal body temperature','cycle'),
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
    # Daily and per-meal nutrient totals are the *logged* Yazio macros.
    "INTAKE_ON": """
        SELECT day AS src, key AS dst, amount FROM (
            SELECT log_date AS day, sum(kcal_total) AS energy, sum(protein_total) AS protein,
                   sum(carbs_total) AS carbohydrate, sum(fat_total) AS fat
            FROM yazio_log GROUP BY log_date
        ) UNPIVOT (amount FOR key IN (energy, protein, carbohydrate, fat))
    """,
    "LOGGED_ON": "SELECT DISTINCT log_date || '|' || meal AS src, log_date AS dst FROM yazio_log",
    "CONTAINS": """
        SELECT id AS src, key AS dst, amount FROM (
            SELECT log_date || '|' || meal AS id, sum(kcal_total) AS energy,
                   sum(protein_total) AS protein, sum(carbs_total) AS carbohydrate,
                   sum(fat_total) AS fat
            FROM yazio_log GROUP BY log_date, meal
        ) UNPIVOT (amount FOR key IN (energy, protein, carbohydrate, fat))
    """,
    "COMPOSED_OF": """
        SELECT fdc_id AS src, ingredient_key AS dst, gram_weight
        FROM food_ingredients
    """,
    # The reconciled link: a logged meal → its canonical USDA foods (portion grams).
    "EATEN": """
        SELECT l.log_date || '|' || l.meal AS src, m.fdc_id AS dst,
               sum(l.amount) AS grams, sum(l.portions) AS portions
        FROM yazio_log l JOIN food_map m ON l.product = m.product
        WHERE m.fdc_id IS NOT NULL AND m.fdc_id IN (SELECT fdc_id FROM foods)
        GROUP BY 1, m.fdc_id
    """,
    # Each USDA food → its per-100g nutrient profile (macros + key micros).
    "HAS_NUTRIENT": "SELECT u.fdc_id AS src, m.nkey AS dst, u.per_100g FROM ("
        "SELECT fdc_id, col, per_100g FROM foods UNPIVOT (per_100g FOR col IN ("
        + ", ".join(c for c, _k, _n, _u, _cat in NUTRIENT_COLS) + "))) u JOIN (VALUES "
        + ", ".join(f"('{c}', '{k}')" for c, k, _n, _u, _cat in NUTRIENT_COLS)
        + ") m(col, nkey) ON u.col = m.col WHERE u.per_100g IS NOT NULL",
    "OBSERVED_ON": f"""
        SELECT CASE metric {' '.join(f"WHEN '{m}' THEN '{s}'" for m, s in _SYMPTOMS.items())} END AS src,
               {_LOCAL_DAY} AS dst, any_value(value_label) AS value
        FROM measurements_categorized
        WHERE metric IN ({', '.join(f"'{m}'" for m in _SYMPTOMS)})
        GROUP BY 1, 2
        UNION ALL
        SELECT 'bbt' AS src, {_LOCAL_DAY} AS dst,
               CAST(round(avg(value_num), 2) AS VARCHAR) || ' °C' AS value
        FROM measurements WHERE metric = 'BasalBodyTemperature' GROUP BY 2
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
