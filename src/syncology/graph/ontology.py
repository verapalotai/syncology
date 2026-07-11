"""Knowledge-graph ontology — node and relationship schema for Kuzu.

The graph is the integration layer over the DuckDB marts: a ``Day`` spine that
ties together lab biomarkers, cycle phase, nutrition, and activity, so a single
traversal can answer cross-domain questions (e.g. a hormone's reference range by
inferred cycle phase). Node types follow the A1 handoff: Biomarker, Nutrient,
Food, Ingredient, CyclePhase, Symptom, Activity, plus LabResult (a measurement),
Day, and ReferenceRange.

``Ingredient`` is modeled but not yet populated — it awaits the food-composition
lookup (USDA / Open Food Facts). ``Symptom`` and ``Food`` are populated from the
data on hand (cycle signs; Yazio meal correlations), so they are real but sparse.
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
    "CREATE NODE TABLE ReferenceRange(id STRING, low DOUBLE, high DOUBLE, unit STRING, "
    "PRIMARY KEY(id))",
    "CREATE NODE TABLE Nutrient(key STRING, name STRING, unit STRING, PRIMARY KEY(key))",
    "CREATE NODE TABLE Food(id STRING, source STRING, logged_ts TIMESTAMP, PRIMARY KEY(id))",
    "CREATE NODE TABLE Ingredient(key STRING, name STRING, PRIMARY KEY(key))",
    "CREATE NODE TABLE Symptom(key STRING, name STRING, category STRING, PRIMARY KEY(key))",
    "CREATE NODE TABLE Activity(id STRING, activity_type STRING, start_ts TIMESTAMP, "
    "duration_s DOUBLE, distance_km DOUBLE, PRIMARY KEY(id))",
)

REL_TABLES: tuple[str, ...] = (
    "CREATE REL TABLE MEASURED_AS(FROM LabResult TO Biomarker)",
    "CREATE REL TABLE RESULT_ON(FROM LabResult TO Day)",
    "CREATE REL TABLE REF_FOR(FROM ReferenceRange TO Biomarker)",
    "CREATE REL TABLE IN_PHASE(FROM Day TO CyclePhase)",
    "CREATE REL TABLE PERFORMED_ON(FROM Activity TO Day)",
    "CREATE REL TABLE INTAKE_ON(FROM Day TO Nutrient, amount DOUBLE)",
    "CREATE REL TABLE FOOD_LOGGED_ON(FROM Food TO Day)",
    "CREATE REL TABLE CONTAINS(FROM Food TO Nutrient, amount DOUBLE)",
    "CREATE REL TABLE COMPOSED_OF(FROM Food TO Ingredient)",
    "CREATE REL TABLE OBSERVED_ON(FROM Symptom TO Day, value STRING)",
)


NODE_NAMES: tuple[str, ...] = (
    "Day", "CyclePhase", "Biomarker", "LabResult", "ReferenceRange",
    "Nutrient", "Food", "Ingredient", "Symptom", "Activity",
)
REL_NAMES: tuple[str, ...] = (
    "MEASURED_AS", "RESULT_ON", "REF_FOR", "IN_PHASE", "PERFORMED_ON",
    "INTAKE_ON", "FOOD_LOGGED_ON", "CONTAINS", "COMPOSED_OF", "OBSERVED_ON",
)


def create_schema(conn) -> None:
    """Create all node and relationship tables on a fresh Kuzu connection."""
    for ddl in NODE_TABLES + REL_TABLES:
        conn.execute(ddl)
