# Syncology Warehouse — Schema & Data Dictionary

The A1 warehouse is a single local DuckDB file (`data/clean/syncology.duckdb`,
gitignored). This document is the data dictionary for its tables, views, and the
derivation lineage between them. All counts below are aggregate row counts
(public-safe); no individual health values appear here.

## Conventions

- **Long/tidy core.** Health measurements live in one `measurements` table with
  the metric held as a *value* (`metric` column), not as a column or table name.
  A previously-unseen record type (e.g. the ones an Apple Watch will add) ingests
  with no schema change.
- **Identity & idempotency.** Each measurement's `row_key` is a hash of its
  natural key `(metric, source, start, end, value)`, used as the PRIMARY KEY.
  Re-running any loader inserts only genuinely new rows.
- **Day grain.** Marts bucket by *local* calendar date
  (`start_ts AT TIME ZONE 'Europe/Budapest'`), so a 23:00 reading lands on the
  correct day rather than rolling into the next UTC day. Instants are stored in
  UTC; the timezone is a build parameter.
- **Derived objects are rebuildable.** Views and the `cycle_phases` /
  `category_value_map` / `metric_catalog` tables are (re)built idempotently from
  `measurements` + `activity_summary`.

## Lineage

```mermaid
flowchart LR
  XML[exportación.xml\n232 MB] -->|streaming parse| M[(measurements)]
  XML -->|streaming parse| AS[(activity_summary)]
  M --> MC[(metric_catalog)]
  CVM[(category_value_map)] --> CAT[/measurements_categorized/]
  M --> CAT
  CAT --> DA[/daily_activity/]
  AS --> DA
  CAT --> DN[/daily_nutrition/]
  CAT --> CD[/cycle_days/]
  CD -->|sympto-thermal inference| CP[(cycle_phases)]
  CD -->|sympto-thermal inference| CS[(cycle_summary)]

  classDef tbl fill:#e8f0fe,stroke:#4285f4;
  classDef view fill:#e6f4ea,stroke:#34a853;
  class M,AS,MC,CVM,CP,CS tbl;
  class CAT,DA,DN,CD view;
```

Rectangles with square brackets are base tables; parallelograms (slashes) are
SQL views. Module map: parser → `src/syncology/ingest/apple_health.py`;
normalization → `transform/category_values.py`; marts + phase inference →
`transform/marts.py`.

---

## Base tables

### `measurements` — the tidy measurement store (461,060 rows)

One row per unique health measurement across all sources.

| column | type | notes |
|---|---|---|
| `row_key` | VARCHAR **PK** | sha1 of `(metric, source, start, end, value)` — dedup + idempotency |
| `metric` | VARCHAR **NN** | HealthKit type with the `HK…TypeIdentifier` prefix stripped, e.g. `StepCount` |
| `record_kind` | VARCHAR | `Quantity` / `Category` / … (the stripped prefix) |
| `value_num` | DOUBLE | numeric measurements (460,863 rows) |
| `value_str` | VARCHAR | categorical enum strings (197 rows), normalized in the categorized view |
| `unit` | VARCHAR | e.g. `kcal`, `km`, `count`, `degC` |
| `start_ts` | TIMESTAMPTZ **NN** | UTC instant; local date derived at query time |
| `end_ts` | TIMESTAMPTZ | interval end for ranged samples |
| `creation_ts` | TIMESTAMPTZ | when the record was written by the source app |
| `source` | VARCHAR **NN** | `Veronika's iPhone`, `Yazio`, `Tempdrop`, `Slopes`, `Salud` |
| `source_version` | VARCHAR | app/OS version string |
| `correlation_id` | VARCHAR | groups records that belong to one logged event (e.g. a meal); 81,106 rows linked to 5,968 correlations |

Coverage: 2021-12-24 → 2026-06-24, 59 distinct metrics. Raw export had ~542k
`Record` elements; dedup to the natural key (Apple writes each Yazio nutrient
record both standalone and inside its meal `Correlation`) yields 461,060 unique
rows. See `docs/apple_health_ingestion_report.md` for the full story.

### `activity_summary` — Apple Activity rings (722 rows)

One row per day, keyed by `date_components` (a `YYYY-MM-DD` string, PK).
Columns: `active_energy(_goal/_unit)`, `move_time(_goal)`,
`exercise_time(_goal)`, `stand_hours(_goal)` — all DOUBLE except the unit.

### `category_value_map` — HealthKit enum → label + ordinal (22 rows)

Auditable entity-resolution table. PK `(metric, raw_value)`.

| column | type | notes |
|---|---|---|
| `metric` | VARCHAR **NN** | e.g. `MenstrualFlow`, `CervicalMucusQuality` |
| `raw_value` | VARCHAR **NN** | raw HealthKit enum, e.g. `HKCategoryValueVaginalBleedingMedium` |
| `label` | VARCHAR **NN** | clean label, e.g. `medium`, `egg_white`, `present` |
| `ordinal` | INTEGER | rank where the category is meaningfully ordered (flow intensity, mucus fertility signal); NULL for nominal / presence-only values |

### `cycle_phases` — inferred phase + fertility zone per day (601 rows)

Materialized from `cycle_days`. PK `day`.

| column | type | notes |
|---|---|---|
| `day` | DATE **PK** | local calendar date |
| `phase` | VARCHAR **NN** | clinical: `menstruation` / `follicular` / `ovulation` / `luteal` / `unknown` |
| `fertility_zone` | VARCHAR **NN** | STM zones: `infertile_pre` / `fertile` / `infertile_post` / `unknown` |
| `fertile_window` | BOOLEAN **NN** | shorthand for `fertility_zone = 'fertile'` |
| `cycle_day` | INTEGER | 1-based day within the current cycle, NULL outside a detected cycle |

**Method — sympto-thermal (STM), conservative with explicit `unknown`.**
Implements the rules of a sympto-thermal fertility-awareness course, cross-checking
the two primary biomarkers. PMOS cycles are often long / irregular / anovulatory,
so nothing is fabricated — unconfirmed stretches stay `unknown`.

- **Temperature is the only confirmer of ovulation.** Coverline = the highest of
  the 6 low readings; need 3 readings above it, the **3rd ≥ 0.2 °C** above (course
  value). *Exception 1:* if the 3rd isn't a full 0.2, a **4th above the line**
  confirms. *Exception 2:* a reading dipping to/below the line voids that run
  (conservative). Ovulation = the last low day before the rise.
- **Mucus predicts.** Peak day (`csúcsnap`) = last peak-type mucus day (watery /
  egg-white); mucus confirms at **peak + 3**. Before ovulation, *any* mucus is
  fertile (change point = first mucus).
- **Cross-check:** confirmation day = the **later** of temperature-confirm and
  peak + 3 (temperature still required). Fertility zones follow: `infertile_pre`
  (menses → first mucus), `fertile` (first mucus → confirmation), `infertile_post`
  (after confirmation).

*Scope note:* the course's advanced early-infertile counting rules (−21-day,
Döring −8, 5/3-day) are contraceptive-precision rules needing 6–12 logged cycles;
they are intentionally **not** implemented — `infertile_pre` is simply menses →
first mucus.

On the real data (coverline margin 0.2 °C): **15/20 cycles ovulatory (75%)**,
16% `unknown`, biphasic BBT confirmed (follicular ≈ 36.47 °C < luteal ≈ 36.67 °C).
Detection is stable across 0.15–0.30 °C (the proper coverline rule is robust to
the margin), so the course's 0.2 needs no tuning; see
`scripts/sweep_ovulation_threshold.py`.

### `cycle_summary` — one row per cycle, with anomaly flags (20 rows)

Materialized from the same inference. PK `cycle_number`.

| column | type | notes |
|---|---|---|
| `cycle_number` | INTEGER **PK** | 1-based in observed order |
| `cycle_start` / `next_start` | DATE | menses onsets bounding the cycle |
| `cycle_length_days` | INTEGER | `start → next_start`; NULL for the censored last cycle |
| `ovulation_day` | DATE | last low day before the temperature rise (NULL if anovulatory) |
| `temp_confirm_day` | DATE | day the temperature run confirms (3rd/4th high) |
| `peak_day` | DATE | last peak-type mucus day (`csúcsnap`) |
| `confirmation_day` | DATE | cross-checked: `max(temp_confirm, peak + 3)` |
| `follicular_days` / `luteal_days` | INTEGER | phase lengths around a detected ovulation |
| `short_luteal` | BOOLEAN **NN** | luteal ≤ 10 days — the course's low-progesterone flag |
| `cycle_length_z` / `luteal_length_z` | DOUBLE | z-score vs population norms |
| `cycle_length_flag` / `luteal_length_flag` | BOOLEAN | |z| > 2σ (NULL if length unknown) |
| `anovulatory` | BOOLEAN **NN** | no temperature-confirmed ovulation |

Norms (for z-scores): cycle length μ=29.30, σ=3.89 (n=1665); luteal length
μ=13.27, σ=2.67 (n=1514), from Fehring et al. (2012) menstrual-cycle-phase data.
On the real data 7/20 cycles flag — short luteals (5–7 d, low-progesterone signal)
and very long cycles (up to 67 d, +9.7σ). A flagged *long* luteal (> ~16 d) usually
means a false-early thermal shift in a long cycle, so the flag doubles as a
detection-quality check.

### `metric_catalog` — per-metric summary (59 rows)

Convenience rollup rebuilt each parse: `metric`, `record_kind`, `n_rows`,
`n_sources`, `first_ts`, `last_ts`, `units`.

---

## Views

### `measurements_categorized` (461,060 rows)

`measurements` LEFT JOINed to `category_value_map` on `(metric, value_str)`,
adding `value_label` and `value_ordinal`. Numeric rows pass through with both
NULL. All marts read from this view rather than raw `measurements`.

### `daily_activity` (1,643 days)

Per local day: `steps`, `distance_km`, `active_energy_kcal`,
`basal_energy_kcal`, `flights_climbed` (summed from `measurements`) FULL-OUTER
joined to `activity_summary` for `exercise_min`, `stand_hours`, `move_time`.

### `daily_nutrition` (262 days)

Per local day from the `Dietary*` metrics: `energy_kcal`, `protein_g`,
`carbs_g`, `fat_g`, `fiber_g`, `sugar_g`, and `meals_logged`
(`count(DISTINCT correlation_id)`). ~1 yr of Yazio coverage.

### `cycle_days` (601 days)

Per local day: `bbt_c` (avg BasalBodyTemperature), `flow_ordinal`,
`mucus_ordinal`, `intermenstrual_bleeding`, `lh_surge`. The raw signal layer that
feeds phase inference. ~1.8 yr of Tempdrop coverage.

---

## Example cross-domain queries

Average BBT by inferred cycle phase (the A1 definition-of-done query):

```sql
SELECT p.phase, count(d.bbt_c) AS n_days, round(avg(d.bbt_c), 3) AS avg_bbt_c
FROM cycle_phases p JOIN cycle_days d USING (day)
WHERE d.bbt_c IS NOT NULL
GROUP BY p.phase ORDER BY avg_bbt_c;
```

Protein intake on active vs. quiet days (nutrition × activity overlap ≈ 1 yr).
Note: without an Apple Watch the Activity `exercise_min`/`stand_hours` rings are
empty, so step count is the reliable activity proxy for now — the watch (≈ Aug
2026) will fill those rings through the same pipeline:

```sql
SELECT (a.steps >= 10000) AS active_day, round(avg(n.protein_g), 1) AS avg_protein_g
FROM daily_nutrition n JOIN daily_activity a USING (day)
WHERE n.protein_g IS NOT NULL AND a.steps IS NOT NULL
GROUP BY 1;
```

## Rebuild

```bash
uv run python scripts/parse_apple_health.py      # measurements + activity_summary + metric_catalog
uv run python scripts/normalize_categories.py    # category_value_map + measurements_categorized
uv run python scripts/build_marts.py             # daily_* + cycle_days + cycle_phases
```
