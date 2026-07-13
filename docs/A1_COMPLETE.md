# A1 — Ingestion + Personal Knowledge Graph: Complete

A1 turned ~4.5 years of raw, multi-source, multi-language personal health data into
a clean, queryable **DuckDB warehouse** and a **Kuzu knowledge graph**, with entity
resolution across biomarkers and foods. This is the status summary; the data
dictionary is `docs/schema.md` and the deep-dives are the build reports below.

All figures here are aggregate counts (public-safe). No individual health values
appear in the repo — real data lives under gitignored `data/`, and every commit
was checked against the privacy wall.

## Deliverables (handoff §5 order of work)

| # | Deliverable | Status | Key artifacts |
|---|---|---|---|
| 1 | Env setup (uv, `.env.example`) | ✅ | `pyproject.toml`, `config.py` |
| 2 | Streaming Apple Health → DuckDB parser (type-agnostic, idempotent) | ✅ | `ingest/apple_health.py` — 461,060 rows, 59 metric types, bulk columnar load |
| 3 | Category-value normalization | ✅ | `transform/category_values.py` → `measurements_categorized` |
| 4 | Daily marts + cycle-phase inference | ✅ | `transform/marts.py` — sympto-thermal engine (coverline rule, mucus cross-check, false-shift guard) |
| 5 | Lab PDF → structured biomarkers (Pydantic) | ✅ | `ingest/labs.py` — 755 rows / 22 panels; local-model + API paths |
| 6 | Biomarker entity resolution (write-up #1) | ✅ | `resolve/biomarkers.py` + `resolve/llm.py` (rule-vs-LLM eval) + `resolve/reference_ranges.py` (temporal) |
| 7 | Ontology + Kuzu graph | ✅ | `graph/ontology.py`, `graph/build.py` — 11 node types, 10 edge types |
| 8 | Food/nutrient lookup (USDA + Yazio reconciliation) | ✅ | `ingest/…`, `resolve/foods.py` — 13.7k foods, 2.3k ingredients, cross-lingual reconciliation |
| 9 | Schema diagram + data dictionary | ✅ | `docs/schema.md` |

Beyond the numbered steps: **Strava + Apple-workout ingest** → unified `activities`
table → graph `Activity` nodes (the handoff lists `Activity`; the data for it
didn't exist until this).

## Definition of done (§3) — met and verified

- **DuckDB cross-domain query** — "average BBT by cycle phase" (clean biphasic
  signal: follicular ≈ 36.50 °C < luteal ≈ 36.83 °C); "protein on active vs quiet
  days."
- **Kuzu graph traversal** — `biomarker → reference range → cycle phase`
  (temporally correct: a result is judged against the reference interval in effect
  on its date); `Food → COMPOSED_OF → Ingredient` composition.
- **60 tests** pass across 8 modules; ruff clean.

## The warehouse & graph at a glance

**DuckDB** — 17 base tables + 7 views:
- `measurements` (461,060) · `measurements_categorized` · `metric_catalog` (59)
- `cycle_days` · `cycle_phases` (601) · `cycle_summary` (20; 14 ovulatory)
- `daily_activity` (1,643) · `daily_nutrition` (262) · `activities` (147) · `workouts` (2)
- `lab_results` (755; 22 panels) · `biomarker_registry` (84) · `biomarker_map`
  (148 raw names) · `biomarker_reference_ranges` (83 eras; 5 biomarkers changed) ·
  `lab_results_canonical` · `lab_results_ranged`
- `foods` (13,692) · `ingredients` (2,332) · `food_ingredients` (18,584) ·
  `yazio_foods` (875) · `yazio_log` (5,245) · `food_map` (875 reconciled)

**Kuzu graph** — a `Day` spine integrating everything:
- Nodes: Day (1,647) · CyclePhase · Biomarker (84) · LabResult (755) ·
  ReferenceRange (83) · Nutrient (16) · Food (13,692) · Ingredient (2,332) ·
  Meal (844) · Symptom (4) · Activity (147)
- Edges: MEASURED_AS · RESULT_ON · REF_FOR · IN_PHASE · PERFORMED_ON ·
  INTAKE_ON · LOGGED_ON · CONTAINS · **EATEN** (Meal→Food, 4,618 — the reconciled
  link) · **HAS_NUTRIENT** (Food→Nutrient, 207,710) · COMPOSED_OF (18,584) · OBSERVED_ON
- The nutrition layer is the Yazio named log resolved to USDA: traversing
  `Meal-EATEN→Food-HAS_NUTRIENT→Nutrient` derives nutrients never logged (e.g. daily
  magnesium). The `food_map` reconciliation is what powers `EATEN`.

## Two entity-resolution regimes (the write-up story)

A1 produced two contrasting resolution problems, and the honest finding is that the
right method depends on the regime:

- **Biomarkers** — small closed vocabulary (84 canonical). A **rule-based**
  resolver (normalization + alias dictionary + specimen disambiguation) hits 100%
  coverage; the LLM adds nothing. Plus a **bitemporal** twist: reference intervals
  change over time, so results are judged as-of their date.
- **Foods** — open, cross-lingual vocabulary (875 → 13.7k). **Rules are useless**;
  a **learned embeddings + LLM-translation** pipeline is essential. The benchmark
  (`docs/food_reconciliation_report.md`) shows the query transform (translation)
  dominates the embedder choice and reranking — a result that pushes back on
  "pick the top MTEB model."

## Reports

- `docs/schema.md` — data dictionary (tables, views, graph, lineage, rebuild).
- `docs/apple_health_ingestion_report.md` — the parser build & the 45-min→150s fix.
- `docs/cycle_marts_report.md` — the sympto-thermal cycle model, end to end.
- `docs/food_reconciliation_report.md` — the cross-lingual embedding benchmark.

## Reproduce

Full rebuild sequence is in `docs/schema.md` → *Rebuild*. Sources live under
gitignored `data/raw/personal/` (health data) and `data/raw/public/` (USDA FDC).

## Deferred / out of scope for A1

- **Write-up #1** — publishing the entity-resolution work on the Quarto blog. The
  material (biomarker rule-vs-LLM eval, food embedding benchmark, reference-range
  finding) is all in place; this is authoring, and is the **paper we continue with**.
- **Demo-persona generation** and the **public benchmark corpus** — A0/A2 concerns
  per the handoff.
- **Apple Watch data** (`SleepAnalysis`, `HeartRate`, `HRV`) — arrives ≈ Aug 2026
  and ingests through the same type-agnostic pipeline with no code changes.
