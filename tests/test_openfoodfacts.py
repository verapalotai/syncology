"""Tests for the Open Food Facts second-corpus ingest."""

from __future__ import annotations

import duckdb

from syncology.ingest import openfoodfacts as off

_COLS = ("code\tproduct_name\tbrands\tcategories_tags\tcountries_tags\t"
         "energy-kcal_100g\tproteins_100g\tfat_100g\tcarbohydrates_100g")
_ROWS = [
    "1\tLecsó\t\ten:vegetables\ten:hungary\t40\t1.5\t0.3\t6",
    "2\tAjvar\tDemeter\ten:spreads\ten:germany,en:austria\t80\t2\t5\t7",
    "3\tOnly USA\tActa\ten:snacks\ten:united-states\t500\t5\t20\t60",  # filtered out
    "4\t \t\t\ten:hungary\t\t\t\t",  # empty name → dropped
    "5\tLecsó\t\ten:vegetables\ten:hungary\t41\t1.4\t0.3\t6",  # dup (name,brand) → collapsed
]


def _write_csv(path):
    path.write_text(_COLS + "\n" + "\n".join(_ROWS) + "\n")
    return str(path)


def test_off_build_filters_countries_and_dedups(tmp_path):
    csv = _write_csv(tmp_path / "off.csv")
    con = duckdb.connect(":memory:")
    n = off.build(con, csv)
    names = {r[0] for r in con.execute("SELECT description FROM off_foods").fetchall()}
    assert names == {"Lecsó", "Ajvar"}  # USA excluded, empty dropped, dup collapsed
    assert n == 2
    # macros parsed to DOUBLE
    kcal = con.execute("SELECT energy_kcal FROM off_foods WHERE description='Ajvar'").fetchone()[0]
    assert kcal == 80.0


def test_off_build_respects_country_arg(tmp_path):
    csv = _write_csv(tmp_path / "off.csv")
    con = duckdb.connect(":memory:")
    off.build(con, csv, countries=("germany",))
    names = {r[0] for r in con.execute("SELECT description FROM off_foods").fetchall()}
    assert names == {"Ajvar"}  # only the germany/austria row
