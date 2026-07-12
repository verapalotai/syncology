"""Tests for the Yazio nutrition-log ingest (synthetic CSV)."""

from __future__ import annotations

import duckdb
import pytest

from syncology.ingest import yazio


@pytest.fixture
def log_csv(tmp_path):
    p = tmp_path / "nutrition_log.csv"
    p.write_text(
        "Date,Time,Meal,Product,Amount,Unit,Portions,Calories/g,Calories total,"
        "Protein/g,Fat/g,Carbs/g,Protein total,Fat total,Carbs total\n"
        "2025-05-01,08:00,Breakfast,Alma,100,g,1,0.52,52,0.003,0.002,0.14,0.3,0.2,14\n"
        "2025-05-02,08:00,Breakfast,Alma,150,g,1,0.52,78,0.003,0.002,0.14,0.45,0.3,21\n"
        "2025-05-01,12:00,Lunch,Csirkemell,200,g,1,1.65,330,0.31,0.036,0,62,7.2,0\n"
    )
    return p


def test_builds_log_and_distinct_foods(log_csv):
    con = duckdb.connect(":memory:")
    n = yazio.build(con, log_csv)
    assert n == 2  # Alma, Csirkemell
    assert con.execute("SELECT count(*) FROM yazio_log").fetchone()[0] == 3


def test_per_100g_macros_averaged(log_csv):
    con = duckdb.connect(":memory:")
    yazio.build(con, log_csv)
    alma = con.execute(
        "SELECT energy_kcal, carbs_g, times_logged FROM yazio_foods WHERE product = 'Alma'"
    ).fetchone()
    assert alma == (52.0, 14.0, 2)  # 0.52*100 kcal, 0.14*100 carbs, logged twice
