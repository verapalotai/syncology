# Apple Health Ingestion — Build & Debug Report

A narrative, reproducible account of building and hardening the Apple Health
export parser for Syncology. Written to be read end-to-end: it explains *what*
was built, *why* each decision was made, *what broke*, and *how it was proven
correct*. Numbers and previews are from the real export (aggregate counts only —
no individual health values are reproduced here).

---

## 1. What this component does

Syncology ingests a personal health-data export from Apple Health into a local
DuckDB warehouse, which later feeds a knowledge graph + agentic copilot. The
Apple Health export is a single XML file:

```
data/raw/personal/activity/apple_health_export/exportación.xml   (232 MB)
```

The parser streams that file into two tables in
`data/clean/syncology.duckdb`:

- **`measurements`** — one tidy/long row per health measurement (steps, heart
  rate, calories, body temperature, menstrual flow, …). The metric name is a
  *value in a column*, not a table or column name.
- **`activity_summary`** — one row per day of Apple's Activity ring data.

A companion script (`scripts/parse_apple_health.py`) runs the parse and prints a
verification report that flags drift against a known-good baseline.

---

## 2. The data model, and why it's shaped this way

### Long/tidy, type-agnostic `measurements`

Apple Health has ~59 distinct metric types in this export, and Apple adds new
ones over time. A wide schema (one column per metric) or a table-per-metric
design would need a migration every time a new `HKQuantityTypeIdentifier…`
appears. Instead the schema is **long/tidy**:

```
row_key         VARCHAR PRIMARY KEY   -- hash of the natural key (dedup + idempotency)
metric          VARCHAR NOT NULL      -- e.g. "StepCount", "BasalBodyTemperature"
record_kind     VARCHAR               -- "Quantity" | "Category" | ...
value_num       DOUBLE                -- numeric measurements
value_str       VARCHAR               -- categorical measurements (e.g. flow level)
unit            VARCHAR
start_ts        TIMESTAMPTZ NOT NULL
end_ts          TIMESTAMPTZ
creation_ts     TIMESTAMPTZ
source          VARCHAR NOT NULL      -- "Veronika's iPhone", "Yazio", ...
source_version  VARCHAR
correlation_id  VARCHAR               -- groups records that belong to one event (e.g. a meal)
```

**Trade-off:** a long table is less immediately queryable than a wide one (you
filter `WHERE metric = 'StepCount'` instead of selecting a column), and every
value is a nullable `DOUBLE`/`VARCHAR` pair. In exchange, ingesting a
previously-unseen metric type needs *zero* schema changes — which matters for an
append-only personal-data pipeline that will run repeatedly over growing
exports. A `metric_catalog` summary table (rebuilt each run) gives back the
"what metrics exist and how big are they" overview that a wide schema would have
made trivial.

### Numeric vs categorical split

Each `Record` has a single `value` attribute that is sometimes a number
(`"1234"`) and sometimes a HealthKit enum string
(`"HKCategoryValueVaginalBleedingLight"`). Rather than store everything as text,
the parser tries `float()` and routes to `value_num` or `value_str`
accordingly. This keeps aggregations (`avg`, `sum`) working directly on
`value_num` without per-query casts. In the real data this split is
460,863 numeric / 197 string / 0 rows with both null.

### `row_key` as identity

`row_key = sha1(metric ␟ source ␟ start ␟ end ␟ value)` is the natural key of a
measurement. Using it as the PRIMARY KEY gives two things at once:

1. **Idempotency** — re-running the parser on an overlapping export inserts only
   genuinely new rows (`INSERT OR IGNORE` skips existing keys).
2. **Dedup** — the export itself contains exact-duplicate records (see §5.3);
   the key collapses them to one row.

---

## 3. The situation inherited

A previous session had a parser that **ran for 45 minutes and never finished.**
The first diagnostic step was to check whether it was alive and making progress:

```bash
pgrep -f parse_apple_health >/dev/null && echo RUNNING || echo FINISHED
ls -la data/clean/                     # db + WAL growing
ps -o pid,etime,%cpu,rss,command -p $(pgrep -f parse_apple_health)
```

Findings:
- Process alive at **45:38 elapsed**, one core pinned (**~178 % CPU** on the
  Python worker), DB at 57 MB with a 12 MB WAL still growing.
- It was *progressing*, just pathologically slowly.

Root cause (diagnosed from the code, not guesswork): the load path did
**row-by-row `executemany` INSERTs** into DuckDB. DuckDB is a columnar store with
a single-writer model; inserting one row per statement hits the primary-key
index individually every time — a well-known anti-pattern. Layered on top were
~1.6 M `dateutil.parser.parse` calls (a slow, format-guessing parser) for
timestamps.

Decision: **kill it and fix the load path**, rather than wait it out.

```bash
kill <pids>; pkill -9 -f parse_apple_health          # force after graceful failed
rm -f data/clean/syncology.duckdb data/clean/syncology.duckdb.wal   # rebuild fresh
```

---

## 4. The rewrite: streaming parse + bulk columnar load

The parser (`src/syncology/ingest/apple_health.py`) is built around three ideas.

### 4.1 Constant-memory streaming with `iterparse`

The XML is hundreds of MB, so it is never fully loaded. `ET.iterparse` fires
`start`/`end` events; each element is `.clear()`-ed after use, and the root is
cleared at every top-level boundary so accumulated children don't grow without
bound:

```python
context = ET.iterparse(str(xml_path), events=("start", "end"))
_, root = next(context)          # consume root start
depth = 1
for event, el in context:
    ...                          # handle Record / Correlation / ActivitySummary
    depth -= 1
    if depth == 1:
        root.clear()             # drop finished top-level subtree
```

**Why `start` *and* `end` events:** `Correlation` groups (meals) wrap nested
`Record` children. Tracking a `correlation_stack` on `start`/`end` lets a nested
record know which correlation it belongs to.

### 4.2 Fast, cached timestamp parsing

Apple's timestamps have a fixed shape: `2024-10-08 19:47:00 +0200`. A
format-specific `strptime` is far faster than `dateutil`, and a cache collapses
the many repeated timestamps to one parse each. `dateutil` is kept only as a
fallback for anything off-format:

```python
_TS_FORMAT = "%Y-%m-%d %H:%M:%S %z"
try:
    dt = datetime.strptime(value, _TS_FORMAT)
except ValueError:
    dt = date_parser.parse(value)      # rare fallback
dt = dt.astimezone(timezone.utc)       # normalize to UTC instant
```

All timestamps are normalized to UTC on the way in. (Note: DuckDB *displays*
`TIMESTAMPTZ` in the session timezone, so a `SELECT` may show
`Europe/Budapest CET` — the stored value is still a single UTC instant.)

### 4.3 Bulk columnar inserts via polars → Arrow

Instead of one INSERT per row, rows accumulate in a Python list and flush in
batches of 50,000 through a **polars DataFrame that DuckDB reads directly** via
its replacement scan:

```python
def flush_measurements() -> None:
    if not batch:
        return
    frame = pl.DataFrame(batch, schema=_FRAME_SCHEMA, orient="row")  # noqa: F841
    con.execute("INSERT OR IGNORE INTO measurements SELECT * FROM frame")
    batch.clear()
```

`frame` looks unused to a linter (`# noqa: F841`) but DuckDB resolves the name
`frame` in the SQL to the local Python variable and pulls it in columnar form —
no per-row round-trips, no manual `?`-placeholder binding. `INSERT OR IGNORE`
makes the load idempotent against the PK.

**Result:** 45 min (unfinished) → **~150–200 s** for the full 232 MB export.

---

## 5. Three problems found while verifying, and their fixes

Verification was not "does it run" but "is the data *correct*." Each of the
following was found by probing, not assumed.

### 5.1 Missing runtime dependencies (`pyarrow`, `pytz`)

The first run after the rewrite failed instantly:

```
_duckdb.Error: ModuleNotFoundError: No module named 'pyarrow'
    ... polars/dataframe/frame.py(1803): to_arrow
```

DuckDB↔polars zero-copy goes through Arrow, which needs `pyarrow`. Added it,
re-ran; the parse then completed but the *report* failed:

```
_duckdb.InvalidInputException: Required module 'pytz' failed to import
    at SELECT min(start_ts), max(start_ts) FROM measurements
```

DuckDB needs `pytz` to render `TIMESTAMPTZ` min/max. Both were added to
`pyproject.toml` as first-class deps (not optional extras — the core load path
requires them):

```bash
uv sync    # + pyarrow==24.0.0, + pytz==2026.2   (polars==1.42.1 from prior session)
```

**Lesson:** "the parse finished" and "the pipeline works" are different claims.
The failure moved *downstream* (insert → report) as each dep was added, which is
exactly what you want — each fix exposed the next real step rather than masking
it.

### 5.2 `activity_summary` doubled — a real idempotency bug in my code

The report flagged `activity_summary rows: 1,444  DRIFT (+100.0%)` against an
expected 722 — suspiciously *exactly* double. Probing confirmed the shape:

```bash
uv run python - <<'PY'
import duckdb
con = duckdb.connect("data/clean/syncology.duckdb")
print(con.execute("SELECT count(*), count(DISTINCT date_components) FROM activity_summary").fetchone())
# -> (1444, 722)   : 722 distinct days, each present exactly twice
print(con.execute("""SELECT cnt, count(*) FROM
  (SELECT date_components, count(*) cnt FROM activity_summary GROUP BY date_components)
  GROUP BY cnt""").fetchall())
# -> [(2, 722)]     : every day appears exactly 2x
```

Cause: `measurements` had a PK and `INSERT OR IGNORE`, but `activity_summary`
plain-appended with no key. Two successful parse runs during debugging stacked
two copies. Fix — give the day its natural key and ignore-on-conflict, mirroring
`measurements`:

```sql
-- db.py schema
date_components VARCHAR PRIMARY KEY   -- was: VARCHAR
```
```python
# apple_health.py
"INSERT OR IGNORE INTO activity_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
```

After the fix, `activity_summary` holds at **722** across re-runs.

**Lesson:** idempotency is a per-table property. Getting it right on the main
table doesn't grant it to the side tables — each writer needs its own conflict
key.

### 5.3 The "duplicate" dietary rows are real — and the correlation link was being silently dropped

The report showed the Yazio nutrition source and every `Dietary*` metric landing
at **~half** their raw count. Two possibilities: (a) the parser is wrongly
dropping distinct meals, or (b) the export genuinely contains duplicates.
Probed the *raw XML* directly:

```bash
uv run python - <<'PY'
# Are same-natural-key Yazio dietary records byte-for-byte identical?
import xml.etree.ElementTree as ET
from collections import Counter
...
# Yazio DietaryEnergyConsumed raw records: 12118
# distinct natural keys: 6052
# keys appearing >1x: 6052  (max multiplicity 4)
#   -> every key appears 2-4x, identical on value/unit/start/end/creationDate
PY
```

They are **byte-for-byte identical across every attribute**, up to 4× — so
collapsing them to one measurement row is *correct* (you must not count the same
500 kcal meal entry four times). The apparent "drift" was the dedup doing its
job; the baseline had simply been built from raw pre-dedup counts.

But probing the DB revealed a subtler issue:

```bash
# 6,059 correlations were parsed, yet:
SELECT count(*) FROM measurements WHERE correlation_id IS NOT NULL;   -- 0
```

`correlation_id` — a whole column the schema was designed around — was **100 %
null.** Digging into the raw structure explained why:

```
Correlation elements: 6,059
child tag counts: {'MetadataEntry': 12118, 'Record': 81188}
example: HKCorrelationTypeIdentifierFood / Yazio / 35 nested Dietary* records
```

Each Yazio **Food correlation wraps ~35 nested nutrient records** (one meal → its
nutrients). And crucially, every nutrient record appears **twice**: once
standalone at top level, once nested inside its meal's `Correlation`. Those are
the "duplicates" from above — standalone + correlation-nested *pairs*, not random
noise. A final probe pinned down document order:

```bash
# first-seen context per dietary key:  {'S': 81348, 'C': 0}
# -> the standalone copy ALWAYS comes first
```

So first-wins dedup deterministically kept the standalone copy (no correlation)
and dropped the correlation-carrying twin — throwing away the meal grouping on
every run.

**Fix (streaming-friendly, no perf regression):** collapsing to one measurement
row is still right, so don't change dedup. Instead, during the stream collect a
compact `row_key → correlation_id` map for every record seen inside a
correlation, then backfill it in **one set-based `UPDATE` join** after the load:

```python
# during parse: link the natural key even when the row came from the standalone twin
if correlation_id is not None:
    corr_of[_row_key(metric, source, start, end, value)] = correlation_id

# after load: one UPDATE, not 81k row updates
UPDATE measurements
SET correlation_id = corr_map.correlation_id
FROM corr_map
WHERE measurements.row_key = corr_map.row_key
```

**Trade-offs considered:**
- *Two-pass / buffer-everything* → breaks constant-memory streaming. Rejected.
- *Per-record UPDATE when the nested twin arrives* → 81k UPDATEs, back to the
  anti-pattern that caused the original 45-min hang. Rejected.
- *Second file pass over just correlations* → correct but ~2× wall time.
  Rejected.
- *In-memory `row_key → corr_id` map + one bulk UPDATE join* → the map is ~81k
  tiny entries (negligible RAM), one columnar UPDATE. **Chosen.**

Result: **81,106 rows linked into 5,968 meals.** Spot-check of one meal shows a
coherent nutrient group (33 rows: energy, protein, carbs, fats, minerals…).

---

## 6. Verification & the drift report

The verification philosophy: a run should be self-checking. `parse_apple_health.py`
prints counts and flags each against a baseline (`_fmt` = OK within 2 %, else
DRIFT). After understanding the data, the baseline was reset from **raw**
pre-dedup numbers to the verified **post-dedup** truth, with a comment explaining
the duplication — so future runs are a genuine regression check rather than a
guaranteed-red wall.

Final clean report (fresh DB):

```
Records seen (raw)      :    542,342   OK      <- matches raw element count
Rows inserted (this run): 461,060
Correlations            : 6,059
Rows linked to a corr.  : 81,106
Workouts                : 2

measurements rows       :    461,060   OK
distinct metric types   :         59   OK
activity_summary rows   :        722   OK
date range              : 2021-12-24 -> 2026-06-24   (expected 2021-12-24 -> 2026-06-24)

Per-source counts:
  Veronika's iPhone         372,362   OK
  Yazio                      87,848   OK
  Tempdrop                      769   OK
  Slopes                         77   OK
  Salud                           4   OK

Metric spot-checks (all OK): BasalBodyTemperature 585, CervicalMucusQuality 104,
  MenstrualFlow 60, DietaryEnergyConsumed 6,052, DietaryProtein 6,047,
  DietaryCarbohydrates 6,052, DietaryFatTotal 6,047
```

**Idempotency proven end-to-end** — a second run on the same DB:

```
Rows inserted (this run): 0
Rows linked to a corr.  : 81,106
measurements rows       :    461,060   OK
activity_summary rows   :        722   OK
```

**Direct integrity probe** (all zero / as-expected):

```
rows with NULL start_ts : 0        end_ts < start_ts    : 0
rows with NULL metric   : 0        future rows (>now)   : 0
rows with NULL source   : 0        both value cols null : 0
numeric-typed rows      : 460,863  string-typed rows    : 197
rows in a correlation   : 81,106
```

Tests: `4 passed`. Ruff: clean.

---

## 7. Files built (annotated)

```
src/syncology/
  db.py                     # DuckDB connection + schema (measurements, activity_summary)
  ingest/
    apple_health.py         # the streaming parser + bulk load + correlation backfill
scripts/
  parse_apple_health.py     # CLI runner + self-checking drift report + metric_catalog
tests/
  test_apple_health.py      # 4 tests over a synthetic export (no real health values)
pyproject.toml              # deps: + pyarrow, + pytz  (polars from prior session)
```

### `src/syncology/db.py` — schema & connection
Single source of truth for the schema. `connect()` creates the parent dir, opens
the file, and runs `CREATE TABLE IF NOT EXISTS`. Both tables use a natural-key
PRIMARY KEY so the whole warehouse is idempotent by construction.

### `src/syncology/ingest/apple_health.py` — the parser
Pure library, no I/O beyond the DB connection passed in. Key surfaces:
- `parse(xml_path, con, batch_size) -> ParseStats` — the entry point.
- `_clean_metric` — strips `HKQuantityTypeIdentifier…` prefixes to a clean metric
  name + record kind.
- `_make_ts_parser` — closure returning a cached UTC timestamp parser.
- `_row_key` / `_correlation_key` — hash-based natural keys.
- `flush_measurements` / `flush_activity` — batched columnar/ignoring inserts.
- correlation backfill — the `corr_of` map + post-load `UPDATE` join.
- `ParseStats` — counts returned for the report (records_seen, rows_inserted,
  correlations, correlated_rows, activity_summaries, workouts).

### `scripts/parse_apple_health.py` — runner + verification
- Resolves the XML path from `SYNCOLOGY_DATA_DIR` (defaults baked in).
- Runs the parse, rebuilds `metric_catalog`, prints the drift report, times it.
- `EXPECTED` holds the verified post-dedup baseline with a comment explaining the
  raw-vs-deduped distinction.

### `tests/test_apple_health.py` — behavior lock-in
Four tests over a small synthetic XML (invented values), covering: all record
types parsed, quantity-vs-category value routing, correlation grouping (2 nested
nutrients share one `correlation_id`, 3 others ungrouped), and idempotent
re-ingest (second pass inserts 0). The synthetic file is named `exportación.xml`
on purpose, to exercise non-ASCII path handling.

---

## 8. Database preview

**`measurements` schema** (12 columns; `row_key` PK, `start_ts`/`metric`/`source`
NOT NULL):

```
row_key VARCHAR PK | metric | record_kind | value_num | value_str | unit
| start_ts TIMESTAMPTZ | end_ts | creation_ts | source | source_version | correlation_id
```

**Sample rows** (earliest by `start_ts`; TZ shown in session-local):

```
DistanceWalkingRunning  0.04378 km   2021-12-24 22:41  Veronika's iPhone   corr=None
StepCount               62      count 2021-12-24 22:41  Veronika's iPhone   corr=None
WalkingSpeed            5.112   km/hr 2021-12-25 09:30  Veronika's iPhone   corr=None
```

**`metric_catalog` (top by row count):**

```
BasalEnergyBurned              Quantity  75,963  1 src  kcal
ActiveEnergyBurned             Quantity  46,533  3 src  kcal
WalkingSpeed                   Quantity  45,057  1 src  km/hr
WalkingStepLength              Quantity  45,057  1 src  cm
WalkingDoubleSupportPercentage Quantity  40,988  1 src  %
StepCount                      Quantity  35,216  1 src  count
DistanceWalkingRunning         Quantity  34,676  1 src  km
```

**A meal (`correlation_id = corr-20ca6d07…`, 33 nutrient rows):**

```
DietaryCalcium            18.512    mg
DietaryCarbohydrates       0.62478  g
DietaryEnergyConsumed      3.827    kcal
DietaryFatMonounsaturated  0.071378 g
DietaryFatSaturated        0.00534  g
...
```

Warehouse on disk: **~111 MB** DuckDB file for 461,060 measurements + 722
activity summaries.

---

## 9. Design decisions & trade-offs (summary table)

| Decision | Alternative | Why chosen |
|---|---|---|
| Long/tidy `measurements` | Wide (column per metric) / table-per-metric | New metric types ingest with no migration |
| `row_key` = hash of natural key as PK | Auto-increment id + separate dedup | Idempotency + dedup in one mechanism |
| `value_num` + `value_str` split | Single text column | Aggregations work without casts |
| polars→Arrow bulk `INSERT OR IGNORE` | Row-by-row `executemany` | 45 min → ~3 min; avoids per-row index hits |
| `strptime` + cache | `dateutil` for every timestamp | Format is fixed; ~1.6 M calls collapse via cache |
| Collapse export duplicates | Keep every raw record | Same measurement written 2–4× must not be counted 2–4× |
| Correlation backfill via 1 UPDATE join | Per-row update / 2nd file pass / buffer all | Keeps streaming + correct grouping, no perf hit |
| `pyarrow`/`pytz` as core deps | Optional extras | The core load + report path requires them |

---

## 10. Lessons learnt

1. **"Slow" can be "broken."** 45 minutes wasn't a big-file problem; it was an
   O(rows) index-per-insert anti-pattern. Diagnose the *shape* of the slowness
   (CPU pinned, WAL crawling) before deciding to wait.
2. **Verify the data, not the exit code.** Every real bug here
   (activity doubling, null correlations) passed "it ran without error." The
   drift report and direct probes are what caught them.
3. **Baselines must match the transform.** Comparing post-dedup output to
   pre-dedup expectations manufactures false drift. Once the data was
   understood, the baseline was reset to the deduped truth.
4. **Duplicates in source data have structure.** The Yazio "duplicates" weren't
   noise — they were standalone+correlation pairs. Understanding *why* a
   duplicate exists changed the fix from "dedup harder" to "dedup, then backfill
   the link."
5. **Idempotency is per-writer.** The side table silently lacked what the main
   table had. Re-runnability has to be designed into every insert path.
6. **Errors that move downstream are good news.** pyarrow→pytz→clean report was a
   healthy progression: each fix uncovered the genuine next step instead of
   hiding it.

---

## 11. How to run / reproduce

```bash
# full parse + verification report (idempotent; safe to re-run)
uv run python scripts/parse_apple_health.py

# fresh rebuild
rm -f data/clean/syncology.duckdb data/clean/syncology.duckdb.wal
uv run python scripts/parse_apple_health.py

# tests + lint
uv run pytest -q
uv run ruff check src scripts
```

Paths/DB location are overridable via `SYNCOLOGY_DATA_DIR` /
`SYNCOLOGY_DUCKDB_PATH` (see `.env.example`).

---

## 12. Open questions / possible next steps

- **`Workout` records (2 seen) are counted but not stored.** If workouts matter
  downstream, they need their own table (route, duration, energy).
- **`correlation_id` is currently unqueryable metadata** — no `correlations`
  table with the meal's own name/timestamp/source. Adding one would let you ask
  "show every nutrient of the meal at 12:00" without a self-join on member rows.
- **`source_version` is stored but unused** — potential signal for
  reconciling device/app upgrades.
- **The 2 % drift tolerance is global.** Small metrics (Salud = 4 rows) would
  flag on a single-row change; per-metric tolerances may be worth it later.
- **Timezone display vs storage** — stored as UTC instants; if the KG cares about
  local wall-clock (e.g. "meals after 8pm"), decide where the local-time
  projection lives.
