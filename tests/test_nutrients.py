"""Tests for the FoodData Central → foods table build (synthetic mini-FDC)."""

from __future__ import annotations

import duckdb
import pytest

from syncology.ingest import nutrients


@pytest.fixture
def fdc_dir(tmp_path):
    # Minimal FDC-shaped CSVs: 2 curated foods + 1 branded (should be excluded).
    (tmp_path / "food.csv").write_text(
        "fdc_id,data_type,description,food_category_id,publication_date\n"
        "1,sr_legacy_food,Banana raw,900,2020-01-01\n"
        "2,foundation_food,Chicken breast,100,2020-01-01\n"
        "3,branded_food,Some US snack,,2020-01-01\n"
    )
    (tmp_path / "food_category.csv").write_text(
        "id,code,description\n900,0900,Fruits\n100,0100,Poultry\n"
    )
    # nutrient_ids: 1008 energy, 1003 protein, 1005 carbs, 1004 fat
    (tmp_path / "food_nutrient.csv").write_text(
        "id,fdc_id,nutrient_id,amount\n"
        "10,1,1008,89\n11,1,1003,1.1\n12,1,1005,23\n"
        "20,2,1008,165\n21,2,1003,31\n22,2,1004,3.6\n"
        "30,3,1008,500\n"  # branded — must not appear
    )
    return tmp_path


def test_builds_only_curated_types(fdc_dir):
    con = duckdb.connect(":memory:")
    n = nutrients.build(con, fdc_dir)
    assert n == 2  # branded excluded
    types = {r[0] for r in con.execute("SELECT DISTINCT data_type FROM foods").fetchall()}
    assert types == {"sr_legacy_food", "foundation_food"}


def test_pivots_nutrients_and_category(fdc_dir):
    con = duckdb.connect(":memory:")
    nutrients.build(con, fdc_dir)
    banana = con.execute(
        "SELECT description, category, energy_kcal, protein_g, carbs_g, fat_g "
        "FROM foods WHERE fdc_id = 1"
    ).fetchone()
    assert banana == ("Banana raw", "Fruits", 89.0, 1.1, 23.0, None)
    chicken = con.execute(
        "SELECT energy_kcal, protein_g, fat_g FROM foods WHERE fdc_id = 2"
    ).fetchone()
    assert chicken == (165.0, 31.0, 3.6)


def test_nutrient_columns_match_registry(fdc_dir):
    con = duckdb.connect(":memory:")
    nutrients.build(con, fdc_dir)
    cols = {c[1] for c in con.execute("PRAGMA table_info('foods')").fetchall()}
    for _nid, (col, _unit) in nutrients.NUTRIENTS.items():
        assert col in cols
