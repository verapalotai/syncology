"""Yazio nutrition-log export → named food log + distinct foods.

The Apple Health export carries Yazio nutrition as *nameless* nutrient bundles
(meal correlations). The standalone Yazio CSV export additionally has the food
**names** and per-gram macros, which is what the food reconciliation resolves
against the USDA ``foods`` table.

Builds ``yazio_log`` (one row per logged item) and ``yazio_foods`` (distinct
product with representative per-100g macros — the reconciliation source, ~875
cross-language names).
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def build(con: duckdb.DuckDBPyConnection, csv_path: str | Path) -> int:
    """(Re)build ``yazio_log`` + ``yazio_foods``; return distinct-food count."""
    csv_path = Path(csv_path)
    con.execute("DROP TABLE IF EXISTS yazio_log")
    con.execute(
        f"""
        CREATE TABLE yazio_log AS
        SELECT "Date" AS log_date, "Time" AS log_time, "Meal" AS meal,
               "Product" AS product, "Amount" AS amount, "Unit" AS unit,
               "Portions" AS portions,
               "Calories/g" AS kcal_per_g, "Protein/g" AS protein_per_g,
               "Fat/g" AS fat_per_g, "Carbs/g" AS carbs_per_g,
               "Calories total" AS kcal_total, "Protein total" AS protein_total,
               "Fat total" AS fat_total, "Carbs total" AS carbs_total
        FROM read_csv_auto('{csv_path}')
        WHERE "Product" IS NOT NULL
        """
    )
    con.execute("DROP TABLE IF EXISTS yazio_foods")
    con.execute(
        """
        CREATE TABLE yazio_foods AS
        SELECT product,
               round(avg(kcal_per_g) * 100, 1)  AS energy_kcal,
               round(avg(protein_per_g) * 100, 2) AS protein_g,
               round(avg(fat_per_g) * 100, 2)     AS fat_g,
               round(avg(carbs_per_g) * 100, 2)   AS carbs_g,
               count(*)                            AS times_logged
        FROM yazio_log
        GROUP BY product
        """
    )
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS yazio_foods_pk ON yazio_foods(product)")
    return con.execute("SELECT count(*) FROM yazio_foods").fetchone()[0]
