"""Deterministic tests for qualified Swift call recovery.

The fixture graph is built inline so the file-path nodes match the checkout location.
"""
import json
import sys
from pathlib import Path

import pytest

from graphify_swift_edges.recover import build_symbols, extract_calls, resolve, run, _main

FIX = Path(__file__).parent / "fixtures"
SAMPLE = FIX / "Sample.swift"


def _node(nid, label, file, loc="L1"):
    return {"id": nid, "label": label, "source_file": str(file), "source_location": loc}


def _method_edge(cls, meth):
    return {"source": cls, "target": meth, "relation": "method",
            "confidence": "EXTRACTED", "weight": 1.0}


def _graph():
    """A minimal graph: Caller, ProjectionSvc (owns reconcile+fetch), and TWO Ambig classes."""
    nodes = [
        _node("file_sample", "Sample.swift", SAMPLE),
        _node("cls_caller", "Caller", SAMPLE, "L3"),
        _node("m_caller_dowork", ".doWork()", SAMPLE, "L4"),
        _node("cls_proj", "ProjectionSvc", "/x/ProjectionSvc.swift", "L1"),
        _node("m_proj_reconcile", ".reconcile()", "/x/ProjectionSvc.swift", "L2"),
        _node("m_proj_fetch", ".fetch()", "/x/ProjectionSvc.swift", "L3"),
        # Ambig appears as two distinct class nodes with the same label -> must be skipped
        _node("cls_ambig_a", "Ambig", "/x/AmbigA.swift", "L1"),
        _node("m_ambig_a_foo", ".foo()", "/x/AmbigA.swift", "L2"),
        _node("cls_ambig_b", "Ambig", "/x/AmbigB.swift", "L1"),
        _node("m_ambig_b_foo", ".foo()", "/x/AmbigB.swift", "L2"),
    ]
    edges = [
        {"source": "file_sample", "target": "cls_caller", "relation": "contains", "weight": 1.0},
        _method_edge("cls_caller", "m_caller_dowork"),
        _method_edge("cls_proj", "m_proj_reconcile"),
        _method_edge("cls_proj", "m_proj_fetch"),
        _method_edge("cls_ambig_a", "m_ambig_a_foo"),
        _method_edge("cls_ambig_b", "m_ambig_b_foo"),
    ]
    return {"nodes": nodes, "edges": edges}


# ─────────────────────────── extraction ───────────────────────────

def test_extract_finds_qualified_skips_unqualified():
    calls, nav = extract_calls(SAMPLE.read_bytes())
    pairs = {(c.receiver, c.method) for c in calls}
    assert ("ProjectionSvc", "reconcile") in pairs       # static
    assert ("ProjectionSvc", "fetch") in pairs           # singleton (.shared collapsed)
    assert ("Unknown", "method") in pairs
    assert ("Ambig", "foo") in pairs
    assert ("ProjectionSvc", "missingMethod") in pairs
    # unqualified helper() must NOT appear as a qualified candidate
    assert all(r != "helper" and m != "helper" for r, m in pairs)


def test_enclosing_type_attribution_uses_ast_not_lines():
    calls, _ = extract_calls(SAMPLE.read_bytes())
    # every call sits inside `class Caller`
    assert {c.enclosing_type for c in calls} == {"Caller"}


# ─────────────────────────── resolution ───────────────────────────

def test_resolve_only_emits_safe_edges():
    g = _graph()
    sym = build_symbols(g)
    calls, _ = extract_calls(SAMPLE.read_bytes())
    edges, st = resolve(calls, sym, str(SAMPLE))

    targets = {e["target"] for e in edges}
    assert targets == {"m_proj_reconcile", "m_proj_fetch"}     # exactly the two safe ones
    assert all(e["source"] == "cls_caller" for e in edges)     # attributed to enclosing type
    assert all(e["relation"] == "calls" for e in edges)

    assert st.resolved == 2
    assert st.skip_receiver_unknown == 1        # Unknown.method
    assert st.skip_receiver_ambiguous == 1      # Ambig.foo (two class nodes)
    assert st.skip_method_not_owned == 1        # ProjectionSvc.missingMethod


def test_run_is_idempotent(tmp_path):
    g = _graph()
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(g))
    out = tmp_path / "out.json"

    st1 = run(gp, FIX, out)
    assert st1.edges_after_dedup == 2

    # feeding the augmented graph back in must add nothing (existing-edge guard)
    st2 = run(out, FIX, out)
    assert st2.edges_after_dedup == 0


def test_console_script_invocation(tmp_path, monkeypatch, capsys):
    """Regression: the console-script entry calls _main() with NO args (argv must default)."""
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_graph()))
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["graphify-swift-edges", str(gp), str(FIX), "-o", str(out)])
    assert _main() == 0                                   # no-arg call, exactly as the script does
    assert json.loads(out.read_text())["edges"]


def test_every_emitted_edge_has_provenance(tmp_path):
    g = _graph()
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(g))
    out = tmp_path / "out.json"
    run(gp, FIX, out)
    recovered = [e for e in json.loads(out.read_text())["edges"]
                 if e.get("provenance") == "graphify-swift-edges"]
    assert len(recovered) == 2
    assert all(e["confidence"] == "RESOLVED_QUALIFIED" for e in recovered)
    assert all(e["source_location"].startswith("L") for e in recovered)
