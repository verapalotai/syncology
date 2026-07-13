"""Knowledge-graph ontology — node and relationship schema for Kuzu.

The graph is the integration layer over the DuckDB marts: a ``Day`` spine that
ties together lab biomarkers, cycle phase, nutrition, and activity, so a single
traversal can answer cross-domain questions (e.g. a hormone's reference range by
inferred cycle phase). Node types follow the A1 handoff: Biomarker, Nutrient,
Food, Ingredient, CyclePhase, Symptom, Activity, plus LabResult (a measurement),
Day, and ReferenceRange.

**Food ↔ Meal — the link.** ``Food`` is the *canonical* USDA food entity, composed
of ``Ingredient`` nodes via ``COMPOSED_OF`` and carrying its per-100g nutrient
profile via ``HAS_NUTRIENT`` (USDA FoodData Central). ``Meal`` is a *logged*
consumption event (a Yazio eating occasion: a date + meal type), linked to its
``Day`` (``LOGGED_ON``), to the canonical ``Food``s it consists of with the portion
in grams (``EATEN``), and to its logged macro ``Nutrient`` totals (``CONTAINS``).

``Meal —EATEN→ Food —HAS_NUTRIENT→ Nutrient`` is the join the food-reconciliation
benchmark exists to make possible: the ``food_map`` reconciliation (Yazio product →
USDA ``fdc_id``) populates ``EATEN``, so a logged meal traverses to the full USDA
nutrient profile of its foods — including micronutrients the log itself never
recorded (magnesium consumed, etc. = Σ portion/100 × food's per-100g amount).
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
    # A logged eating occasion (Yazio: a date + meal type) — the consumption side,
    # with the macro totals as logged.
    "CREATE NODE TABLE Meal(id STRING, source STRING, logged_ts TIMESTAMP, "
    "meal_type STRING, kcal DOUBLE, protein_g DOUBLE, fat_g DOUBLE, carbs_g DOUBLE, "
    "PRIMARY KEY(id))",
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
    # The reconciled link: a logged meal → its canonical USDA foods (portion grams),
    # and each food → its per-100g nutrient profile.
    "CREATE REL TABLE EATEN(FROM Meal TO Food, grams DOUBLE, portions DOUBLE)",
    "CREATE REL TABLE HAS_NUTRIENT(FROM Food TO Nutrient, per_100g DOUBLE)",
)


NODE_NAMES: tuple[str, ...] = (
    "Day", "CyclePhase", "Biomarker", "LabResult", "ReferenceRange",
    "Nutrient", "Food", "Meal", "Ingredient", "Symptom", "Activity",
)
REL_NAMES: tuple[str, ...] = (
    "MEASURED_AS", "RESULT_ON", "REF_FOR", "IN_PHASE", "PERFORMED_ON",
    "INTAKE_ON", "LOGGED_ON", "CONTAINS", "COMPOSED_OF", "OBSERVED_ON",
    "EATEN", "HAS_NUTRIENT",
)


def create_schema(conn) -> None:
    """Create all node and relationship tables on a fresh Kuzu connection."""
    for ddl in NODE_TABLES + REL_TABLES:
        conn.execute(ddl)
