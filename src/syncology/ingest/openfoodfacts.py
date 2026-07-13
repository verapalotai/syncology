"""Open Food Facts → a second food corpus (``off_foods``).

USDA FoodData Central is a *generic* vocabulary (SR Legacy / Foundation / FNDDS):
excellent for "banana, raw", but it has no branded or regional European products.
The stratified benchmark localizes the retrieval error tail exactly there — regional
/ out-of-vocabulary foods (`lecsó`, `ajvar`) score 0, branded products (`Oatly
Barista`) are the soft spot. Neither is a ranking problem; the correct row simply
isn't in USDA. This module adds the missing coverage from Open Food Facts, a
crowd-sourced multilingual product database.

We take the flat CSV export (public, ~1.3 GB gz) and keep only products sold in the
countries the logged diet comes from (Hungary / Germany / Austria), so the corpus is
chosen by provenance, not by peeking at the gold labels. Product names are kept in
their original language — an OFF product literally named "Lecsó" is exactly what the
raw Hungarian query should find, without translation.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

# OFF ``countries_tags`` are comma-joined `en:<country>` slugs in the CSV.
DEFAULT_COUNTRIES = ("hungary", "germany", "austria")


def build(
    con: duckdb.DuckDBPyConnection,
    csv_path: str | Path,
    countries: tuple[str, ...] = DEFAULT_COUNTRIES,
) -> int:
    """(Re)build ``off_foods`` from the OFF CSV export; return the row count.

    Keeps products whose ``countries_tags`` include any target country and that
    carry a usable name. Macros are per-100g (nullable — many products lack
    nutrition data, and name coverage is the point). Deduplicated on (name, brand).
    """
    csv_path = Path(csv_path)
    country_pred = " OR ".join(
        f"countries_tags LIKE '%en:{c}%'" for c in countries
    )
    con.execute("DROP TABLE IF EXISTS off_foods")
    con.execute(
        f"""
        CREATE TABLE off_foods AS
        WITH raw AS (
            SELECT
                code AS off_code,
                trim(product_name) AS description,
                nullif(trim(brands), '') AS brands,
                categories_tags AS categories,
                TRY_CAST("energy-kcal_100g" AS DOUBLE) AS energy_kcal,
                TRY_CAST(proteins_100g AS DOUBLE)      AS protein_g,
                TRY_CAST(fat_100g AS DOUBLE)           AS fat_g,
                TRY_CAST(carbohydrates_100g AS DOUBLE) AS carbs_g,
                row_number() OVER (
                    PARTITION BY lower(trim(product_name)), lower(coalesce(brands, ''))
                    ORDER BY code
                ) AS rn
            FROM read_csv(
                '{csv_path}', delim='\t', header=true, quote='',
                all_varchar=true, ignore_errors=true
            )
            WHERE ({country_pred})
              AND product_name IS NOT NULL
              AND length(trim(product_name)) >= 2
        )
        SELECT off_code, description, brands, categories,
               energy_kcal, protein_g, fat_g, carbs_g
        FROM raw WHERE rn = 1
        """
    )
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS off_foods_pk ON off_foods(off_code)")
    return con.execute("SELECT count(*) FROM off_foods").fetchone()[0]
