"""Food/nutrient lookup table from USDA FoodData Central.

Builds a canonical ``foods`` reference table (per-100g macros + key micros) from
the curated FDC food types — SR Legacy, Foundation, and FNDDS survey foods
(~13.7k generic whole/prepared foods with clean nutrient profiles). Branded
foods (1.9M mostly-US products) are skipped for the core table; they are a source
of ``Ingredient`` text handled separately.

This is the DB A5's voice ``log_meal`` resolves free-text meals against, and the
source for the graph's canonical ``Food`` / ``Nutrient`` entities. The large
``food_nutrient.csv`` (~1.8 GB) is scanned once by DuckDB, filtered to the target
nutrients, and pivoted to columns — no row-by-row Python.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

# Curated FDC food types kept in the core table (generic foods, good coverage).
CURATED_TYPES = ("sr_legacy_food", "foundation_food", "survey_fndds_food")

# FDC nutrient_id -> (column, canonical unit). Amounts in food_nutrient are
# per-100g (per-100mL for beverages) for these food types.
NUTRIENTS: dict[int, tuple[str, str]] = {
    1008: ("energy_kcal", "kcal"),
    1003: ("protein_g", "g"),
    1005: ("carbs_g", "g"),
    1004: ("fat_g", "g"),
    1258: ("saturated_fat_g", "g"),
    1079: ("fiber_g", "g"),
    2000: ("sugars_g", "g"),
    1253: ("cholesterol_mg", "mg"),
    1093: ("sodium_mg", "mg"),
    1092: ("potassium_mg", "mg"),
    1087: ("calcium_mg", "mg"),
    1089: ("iron_mg", "mg"),
    1090: ("magnesium_mg", "mg"),
    1114: ("vitamin_d_ug", "ug"),
    1178: ("vitamin_b12_ug", "ug"),
    1177: ("folate_ug", "ug"),
}


def build(con: duckdb.DuckDBPyConnection, fdc_dir: str | Path) -> int:
    """(Re)build the ``foods`` table from an FDC CSV directory; return row count."""
    d = Path(fdc_dir)
    ids = ", ".join(str(i) for i in NUTRIENTS)
    pivots = ",\n            ".join(
        f"max(fn.amount) FILTER (WHERE fn.nutrient_id = {nid}) AS {col}"
        for nid, (col, _unit) in NUTRIENTS.items()
    )
    con.execute("DROP TABLE IF EXISTS foods")
    con.execute(
        f"""
        CREATE TABLE foods AS
        WITH curated AS (
            SELECT fdc_id, description, data_type, food_category_id
            FROM read_csv_auto('{d}/food.csv')
            WHERE data_type IN ({", ".join(f"'{t}'" for t in CURATED_TYPES)})
        ),
        fn AS (
            SELECT fdc_id, nutrient_id, amount
            FROM read_csv_auto('{d}/food_nutrient.csv')
            WHERE nutrient_id IN ({ids})
        ),
        cat AS (SELECT id, description AS name FROM read_csv_auto('{d}/food_category.csv'))
        SELECT
            c.fdc_id,
            c.description,
            c.data_type,
            cat.name AS category,
            {pivots}
        FROM curated c
        JOIN fn ON c.fdc_id = fn.fdc_id
        LEFT JOIN cat ON c.food_category_id = cat.id
        GROUP BY c.fdc_id, c.description, c.data_type, cat.name
        """
    )
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS foods_pk ON foods(fdc_id)")
    return con.execute("SELECT count(*) FROM foods").fetchone()[0]


def build_ingredients(con: duckdb.DuckDBPyConnection, fdc_dir: str | Path) -> tuple[int, int]:
    """Build ``ingredients`` + ``food_ingredients`` from FDC ``input_food.csv``.

    ``input_food`` documents the component foods of composite (Foundation / survey)
    foods — the source for the graph's ``Ingredient`` nodes and ``COMPOSED_OF``
    edges. Returns ``(n_ingredients, n_food_ingredient_links)``.
    """
    d = Path(fdc_dir)
    con.execute("DROP TABLE IF EXISTS food_ingredients")
    con.execute(
        f"""
        CREATE TABLE food_ingredients AS
        SELECT i.fdc_id, i.seq_num,
               lower(trim(i.sr_description)) AS ingredient_key,
               i.sr_description AS ingredient_name,
               i.gram_weight
        FROM read_csv_auto('{d}/input_food.csv') i
        WHERE i.sr_description IS NOT NULL
          AND i.fdc_id IN (SELECT fdc_id FROM foods)
        """
    )
    con.execute("DROP TABLE IF EXISTS ingredients")
    con.execute(
        """
        CREATE TABLE ingredients AS
        SELECT ingredient_key AS key, any_value(ingredient_name) AS name,
               count(DISTINCT fdc_id) AS n_foods
        FROM food_ingredients GROUP BY ingredient_key
        """
    )
    n_ing = con.execute("SELECT count(*) FROM ingredients").fetchone()[0]
    n_link = con.execute("SELECT count(*) FROM food_ingredients").fetchone()[0]
    return n_ing, n_link
