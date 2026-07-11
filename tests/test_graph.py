"""Tests for the knowledge-graph ontology (schema validity + traversals)."""

from __future__ import annotations

import kuzu

from syncology.graph import ontology


def _conn(tmp_path):
    return kuzu.Connection(kuzu.Database(str(tmp_path / "g")))


def test_node_rel_names_match_ddl():
    assert len(ontology.NODE_NAMES) == len(ontology.NODE_TABLES)
    assert len(ontology.REL_NAMES) == len(ontology.REL_TABLES)


def test_schema_creates_all_tables(tmp_path):
    conn = _conn(tmp_path)
    ontology.create_schema(conn)  # must not raise; all DDL valid Kuzu
    res = conn.execute("CALL show_tables() RETURN name")
    tables = set()
    while res.has_next():
        tables.add(res.get_next()[0])
    for name in ontology.NODE_NAMES + ontology.REL_NAMES:
        assert name in tables


def test_biomarker_reference_range_traversal(tmp_path):
    conn = _conn(tmp_path)
    ontology.create_schema(conn)
    conn.execute("CREATE (:Biomarker {key:'tsh', name_en:'TSH', category:'thyroid', unit:'mIU/L'})")
    conn.execute("CREATE (:ReferenceRange {id:'tsh_ref', low:0.5, high:4.8, unit:'mIU/L'})")
    conn.execute(
        "MATCH (r:ReferenceRange {id:'tsh_ref'}), (b:Biomarker {key:'tsh'}) "
        "CREATE (r)-[:REF_FOR]->(b)"
    )
    res = conn.execute(
        "MATCH (r:ReferenceRange)-[:REF_FOR]->(b:Biomarker) RETURN b.key, r.low, r.high"
    )
    assert res.get_next() == ["tsh", 0.5, 4.8]


def test_day_phase_traversal(tmp_path):
    conn = _conn(tmp_path)
    ontology.create_schema(conn)
    conn.execute("CREATE (:CyclePhase {name:'luteal'})")
    conn.execute("CREATE (:Day {date: date('2025-01-01'), phase:'luteal'})")
    conn.execute(
        "MATCH (d:Day), (c:CyclePhase) WHERE d.phase = c.name CREATE (d)-[:IN_PHASE]->(c)"
    )
    res = conn.execute("MATCH (:Day)-[:IN_PHASE]->(c:CyclePhase) RETURN c.name")
    assert res.get_next() == ["luteal"]
