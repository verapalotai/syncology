"""Knowledge-graph ontology — node and relationship schema for Kuzu.

The graph is the integration layer over the DuckDB marts: a ``Day`` spine that
ties together lab biomarkers, cycle phase, nutrition, and activity, so a single
traversal can answer cross-domain questions (e.g. a hormone's reference range by
inferred cycle phase). Node types follow the A1 handoff: Biomarker, Nutrient,
Food, Ingredient, CyclePhase, Symptom, Activity, plus LabResult (a measurement),
Day, and ReferenceRange.

**Food vs Meal.** ``Food`` is the *canonical* USDA food entity (the vocabulary
A5's log_meal resolves against), composed of ``Ingredient`` nodes via
``COMPOSED_OF`` (USDA FoodData Central composition). ``Meal`` is a *logged*
consumption event (a Yazio correlation), linked to its ``Day`` and its
``Nutrient`` totals. Linking a specific ``Meal`` to its canonical ``Food`` is not
yet possible — the Apple Health nutrition export (correlations, unnamed) and the
Yazio CSV (named products) share no key — so the reconciliation (``food_map``)
lives in DuckDB, and the graph carries the canonical Food+Ingredient reference
layer plus the personal Meal/Nutrient/Day layer independently.
"""

from __future__ import annotations

NODE_TABLES: tuple[str, ...] = (
    "CREATE NODE TABLE Day(date DATE, phase STRING, cycle_day INT64, "
    "fertility_zone STRING, PRIMARY KEY(date))",
    "CREATE NODE TABLE CyclePhase(name STRING, PRIMARY KEY(name))",
    "CREATE NODE TABLE Biomarker(key STRING, name_en STRING, category STRING, "
    "unit STRING, PRIMARY KEY(key))",
    "CREATE NODE TABLE LabResult(id STRING, value DOUBLE, unit STRING, flag STRING, "
    "panel_date DATE, PRIMARY KEY(id))",
    # One node per reference-interval *era* per biomarker, each with the validity
    # window it was in effect (valid_to NULL = current). Labs revise intervals
    # over time (SHBG, testosterone, RBC, AMH, MCHC all changed), so a result is
    # judged against the range that was in effect on its date — see REF_FOR and
    # the as-of lookup in resolve/reference_ranges.py.
    "CREATE NODE TABLE ReferenceRange(id STRING, low DOUBLE, high DOUBLE, unit STRING, "
    "n_panels INT64, valid_from DATE, valid_to DATE, PRIMARY KEY(id))",
    "CREATE NODE TABLE Nutrient(key STRING, name STRING, unit STRING, PRIMARY KEY(key))",
    # Canonical food entity (USDA FoodData Central), with per-100g macros — the
    # vocabulary A5's log_meal resolves free text against.
    "CREATE NODE TABLE Food(fdc_id INT64, description STRING, category STRING, "
    "data_type STRING, energy_kcal DOUBLE, protein_g DOUBLE, carbs_g DOUBLE, "
    "fat_g DOUBLE, PRIMARY KEY(fdc_id))",
    # A logged food/meal event (Yazio correlation) — the consumption side.
    "CREATE NODE TABLE Meal(id STRING, source STRING, logged_ts TIMESTAMP, PRIMARY KEY(id))",
    "CREATE NODE TABLE Ingredient(key STRING, name STRING, PRIMARY KEY(key))",
    "CREATE NODE TABLE Symptom(key STRING, name STRING, category STRING, PRIMARY KEY(key))",
    "CREATE NODE TABLE Activity(id STRING, activity_type STRING, start_ts TIMESTAMP, "
    "duration_s DOUBLE, distance_km DOUBLE, energy_kcal DOUBLE, PRIMARY KEY(id))",
)

REL_TABLES: tuple[str, ...] = (
    "CREATE REL TABLE MEASURED_AS(FROM LabResult TO Biomarker)",
    "CREATE REL TABLE RESULT_ON(FROM LabResult TO Day)",
    "CREATE REL TABLE REF_FOR(FROM ReferenceRange TO Biomarker)",
    "CREATE REL TABLE IN_PHASE(FROM Day TO CyclePhase)",
    "CREATE REL TABLE PERFORMED_ON(FROM Activity TO Day)",
    "CREATE REL TABLE INTAKE_ON(FROM Day TO Nutrient, amount DOUBLE)",
    "CREATE REL TABLE LOGGED_ON(FROM Meal TO Day)",
    "CREATE REL TABLE CONTAINS(FROM Meal TO Nutrient, amount DOUBLE)",
    "CREATE REL TABLE COMPOSED_OF(FROM Food TO Ingredient, gram_weight DOUBLE)",
    "CREATE REL TABLE OBSERVED_ON(FROM Symptom TO Day, value STRING)",
)


NODE_NAMES: tuple[str, ...] = (
    "Day", "CyclePhase", "Biomarker", "LabResult", "ReferenceRange",
    "Nutrient", "Food", "Meal", "Ingredient", "Symptom", "Activity",
)
REL_NAMES: tuple[str, ...] = (
    "MEASURED_AS", "RESULT_ON", "REF_FOR", "IN_PHASE", "PERFORMED_ON",
    "INTAKE_ON", "LOGGED_ON", "CONTAINS", "COMPOSED_OF", "OBSERVED_ON",
)


def create_schema(conn) -> None:
    """Create all node and relationship tables on a fresh Kuzu connection."""
    for ddl in NODE_TABLES + REL_TABLES:
        conn.execute(ddl)
