# syncology

A personal health knowledge graph + agentic copilot: chat with three years of your own
labs, cycle, nutrition, and activity data, grounded in a hormone-health knowledge base
(PMOS-focused), with answers you can trace back to the graph nodes and passages that
produced them.

> **Status:** early — knowledge-graph ingestion in progress. Architecture and roadmap below.

## Why

PCOS — renamed **PMOS** (polyendocrine metabolic ovarian syndrome) in May 2026 — affects
~170 million women, and the average patient assembles her own picture from scattered lab
PDFs, cycle apps, and contradictory advice. This project treats that as an ML engineering
problem: structured extraction, entity resolution, retrieval, and model behavior.

## Architecture (planned → built, updated as phases land)

- **Ingestion:** lab PDFs (vision extraction → Pydantic schemas), Apple Health
  (`export.xml` backfill + scheduled Health Auto Export increments), Tempdrop BBT,
  Yazio nutrition, Strava.
- **Knowledge graph:** entity resolution across Hungarian/English biomarker names and
  units; ontology of biomarkers, nutrients, ingredients, cycle phases, symptoms; Kuzu +
  DuckDB.
- **Retrieval:** benchmarked hybrid stack — BM25 + multilingual dense (bge-m3) + graph
  traversal + reranker — with a from-scratch Q/A benchmark, reproducible on an open
  corpus.
- **Agent:** tool layer exposed as a local **MCP server** (works in Claude Desktop
  today), LangGraph orchestration with fast-vs-deliberate routing, eval suite in CI,
  full tracing.
- **Model:** a small open model post-trained (SFT → DPO → GRPO) against a written
  health-advice behavior policy — see the companion Model Behavior Lab repo.

## Privacy by construction

Real health data and the proprietary corpus live under `data/` behind a gitignore
privacy wall (see [data/README.md](data/README.md)) and never enter git history. All
demos and screenshots run on a synthetic demo persona. Public benchmarks run on an open
corpus (PMOS/ESHRE guidelines, PubMed); private-corpus results are reported as
aggregates only.
