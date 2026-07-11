"""Build the Kuzu knowledge graph from DuckDB and run demo traversals.

Prints node/edge counts and a few cross-domain queries (biomarker → reference
range, hormone panels by cycle phase, activity by cycle phase, nutrition by
phase) — the graph half of the A1 definition of done. Aggregates only.

Usage:
    uv run python scripts/build_graph.py [--db DB] [--graph KUZU_DIR]
"""

from __future__ import annotations

import argparse
import os

from syncology import db
from syncology.graph import build, ontology

DEFAULT_GRAPH = os.path.join(
    os.path.dirname(db.DEFAULT_DB_PATH) or ".", "syncology.kuzu"
)


def _rows(conn, cypher: str):
    res = conn.execute(cypher)
    out = []
    while res.has_next():
        out.append(res.get_next())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument("--graph", default=DEFAULT_GRAPH)
    args = ap.parse_args()

    counts = build.build(args.db, args.graph)

    import kuzu

    conn = kuzu.Connection(kuzu.Database(args.graph))

    print("=" * 62)
    print("KNOWLEDGE GRAPH (Kuzu)")
    print("=" * 62)
    print("nodes:", ", ".join(f"{k}={counts[k]}" for k in ontology.NODE_NAMES if counts.get(k)))
    print("edges:", ", ".join(f"{k}={counts[k]}" for k in ontology.REL_NAMES if counts.get(k)))
    modeled = [k for k in ontology.NODE_NAMES + ontology.REL_NAMES if not counts.get(k)]
    if modeled:
        print("modeled, not yet populated:", ", ".join(modeled))

    print("\n[1] Biomarker → ReferenceRange (TSH):")
    for r in _rows(conn,
        "MATCH (rr:ReferenceRange)-[:REF_FOR]->(b:Biomarker {key:'tsh'}) "
        "RETURN b.name_en, rr.low, rr.high, rr.unit"):
        print(f"    {r[0]}: {r[1]}–{r[2]} {r[3]}")

    print("\n[2] Hormone lab panels by cycle phase (biomarker→result→day→phase):")
    q2 = _rows(conn,
        "MATCH (b:Biomarker)<-[:MEASURED_AS]-(:LabResult)-[:RESULT_ON]->(d:Day)"
        "-[:IN_PHASE]->(cp:CyclePhase) "
        "WHERE b.category = 'hormone' "
        "RETURN cp.name, count(*) AS n ORDER BY n DESC")
    for name, n in q2 or []:
        print(f"    {name:<14} {n}")
    if not q2:
        print("    (no hormone panels fell on a phase-tracked day)")

    print("\n[3] Activities by cycle phase (activity→day→phase):")
    for name, n in _rows(conn,
        "MATCH (a:Activity)-[:PERFORMED_ON]->(d:Day)-[:IN_PHASE]->(cp:CyclePhase) "
        "RETURN cp.name, count(*) AS n ORDER BY n DESC"):
        print(f"    {name:<14} {n}")

    print("\n[4] Avg daily protein intake by cycle phase (day→nutrient + day→phase):")
    for name, amt, nd in _rows(conn,
        "MATCH (d:Day)-[i:INTAKE_ON]->(n:Nutrient {key:'DietaryProtein'}), "
        "(d)-[:IN_PHASE]->(cp:CyclePhase) "
        "RETURN cp.name, round(avg(i.amount),1) AS g, count(*) AS days ORDER BY g DESC"):
        print(f"    {name:<14} {amt} g/day   ({nd} days)")
    print("=" * 62)


if __name__ == "__main__":
    main()
