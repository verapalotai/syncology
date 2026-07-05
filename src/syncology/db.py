"""DuckDB connection + schema for the Syncology warehouse.

The tabular store is a single local DuckDB file. Schema is long/tidy: one row
per measurement with the metric held as a column value rather than a table or
column name, so ingesting a previously unseen record type needs no schema change.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = os.environ.get("SYNCOLOGY_DUCKDB_PATH", "data/clean/syncology.duckdb")

# ``row_key`` is a hash of the record's natural key, used as the primary key so
# re-ingesting an overlapping export inserts only genuinely new rows.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS measurements (
    row_key         VARCHAR PRIMARY KEY,
    metric          VARCHAR NOT NULL,
    record_kind     VARCHAR,
    value_num       DOUBLE,
    value_str       VARCHAR,
    unit            VARCHAR,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ,
    creation_ts     TIMESTAMPTZ,
    source          VARCHAR NOT NULL,
    source_version  VARCHAR,
    correlation_id  VARCHAR
);

CREATE TABLE IF NOT EXISTS activity_summary (
    date_components     VARCHAR PRIMARY KEY,
    active_energy       DOUBLE,
    active_energy_goal  DOUBLE,
    active_energy_unit  VARCHAR,
    move_time           DOUBLE,
    move_time_goal      DOUBLE,
    exercise_time       DOUBLE,
    exercise_time_goal  DOUBLE,
    stand_hours         DOUBLE,
    stand_hours_goal    DOUBLE
);
"""


def connect(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open the warehouse (creating its parent directory) and ensure the schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA_SQL)
    return con
