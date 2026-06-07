"""Recover qualified Swift call edges that Graphify's AST pass drops.

Graphify (graphifyy) parses Swift with tree-sitter and records `Class --method--> .foo()`
edges, but it does not link *qualified call sites* (`Type.foo(...)`, `Type.shared.foo(...)`)
back to the callee's method node. The result: services that call each other via a static or
singleton method appear as disconnected islands, so `graphify path`/`affected` can't trace the
pathway.

This post-processor re-parses the source with the *same* parser, resolves qualified calls
deterministically against the graph's own symbol table, and appends the missing `calls` edges.

Scope (honest): recovers TYPE-qualified and SINGLETON-qualified calls only. Instance-variable
calls (`let s = Foo(); s.bar()`) need real type inference (SourceKit / IndexStoreDB) and are out
of scope — that's the documented sequel.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_swift as tss

_SWIFT = Language(tss.language())
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_PASCAL = re.compile(r"^[A-Z]\w*$")

# Swift tree-sitter node types that introduce a named type scope (class/struct/enum/actor/
# extension all parse as `class_declaration`; protocols as `protocol_declaration`).
_TYPE_DECL = {"class_declaration", "protocol_declaration"}

PROVENANCE = "graphify-swift-edges"


# ─────────────────────────── symbol tables from the graph ───────────────────────────

@dataclass
class Symbols:
    class_ids_by_label: dict[str, set[str]]          # "Foo" -> {nodeId, ...} (types that own methods)
    method_id: dict[tuple[str, str], str]            # (classNodeId, "bar") -> methodNodeId
    file_id_by_path: dict[str, str]                  # absolute source path -> file nodeId
    node_label: dict[str, str]                       # nodeId -> label


def _norm_method(label: str) -> str:
    """'.reconcile()' -> 'reconcile'."""
    return label.strip().lstrip(".").split("(")[0].strip()


def build_symbols(graph: dict) -> Symbols:
    edges = graph.get("edges") or graph.get("links") or []
    nodes = graph["nodes"]
    node_label = {n["id"]: n.get("label", "") for n in nodes}

    class_ids_by_label: dict[str, set[str]] = {}
    method_id: dict[tuple[str, str], str] = {}
    # A class node is any node that is the SOURCE of a `method` edge (it owns ≥1 method).
    for e in edges:
        if e.get("relation") != "method":
            continue
        cls, meth = e["source"], e["target"]
        clabel = node_label.get(cls, "")
        if clabel:
            class_ids_by_label.setdefault(clabel, set()).add(cls)
        method_id[(cls, _norm_method(node_label.get(meth, "")))] = meth

    # File nodes: label ends with a code extension and the node id ends with that ext slug.
    file_id_by_path: dict[str, str] = {}
    for n in nodes:
        sf = n.get("source_file", "")
        if sf and n.get("label", "").endswith(".swift") and n.get("source_location") in ("L1", "", None):
            file_id_by_path.setdefault(sf, n["id"])

    return Symbols(class_ids_by_label, method_id, file_id_by_path, node_label)


# ─────────────────────────── qualified-call extraction ───────────────────────────

@dataclass
class Call:
    receiver: str            # "Foo"
    method: str              # "bar"
    enclosing_type: str | None
    line: int                # 1-based


_parser = Parser(_SWIFT)


def _text(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")


def _enclosing_type_name(node, src: bytes) -> str | None:
    cur = node.parent
    while cur is not None:
        if cur.type in _TYPE_DECL:
            name = cur.child_by_field_name("name")
            if name is not None:
                t = _text(src, name).strip()
                return t if _IDENT.match(t) else None
            # extensions: name lives in a user_type / type_identifier child
            for c in cur.children:
                if c.type in ("user_type", "type_identifier"):
                    t = _text(src, c).split(".")[0].strip()
                    return t if _IDENT.match(t) else None
            return None
        cur = cur.parent
    return None


def _segments(callee_text: str) -> list[str] | None:
    parts = [p.strip() for p in callee_text.split(".")]
    if not all(_IDENT.match(p) for p in parts):
        return None
    return parts


def extract_calls(src: bytes) -> tuple[list[Call], int]:
    """Return (qualified calls with a PascalCase receiver, total navigation-callee count)."""
    tree = _parser.parse(src)
    calls: list[Call] = []
    nav_total = 0

    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            callee = n.child_by_field_name("function") or (n.children[0] if n.children else None)
            if callee is not None and callee.type == "navigation_expression":
                nav_total += 1
                segs = _segments(_text(src, callee))
                if segs and len(segs) >= 2 and _PASCAL.match(segs[0]):
                    # accept [Type, method] and [Type, shared, method]; skip deeper/odd chains
                    if len(segs) == 2:
                        receiver, method = segs[0], segs[1]
                    elif len(segs) == 3 and segs[1] == "shared":
                        receiver, method = segs[0], segs[2]
                    else:
                        receiver = method = None
                    if receiver:
                        calls.append(Call(receiver, method,
                                          _enclosing_type_name(n, src),
                                          n.start_point[0] + 1))
        stack.extend(n.children)
    return calls, nav_total


# ─────────────────────────── resolution ───────────────────────────

@dataclass
class Stats:
    files: int = 0
    nav_callees: int = 0
    qualified_candidates: int = 0          # PascalCase receiver
    receiver_known: int = 0                # receiver maps to exactly one class node
    resolved: int = 0                      # + class owns the method -> edge emitted
    skip_receiver_unknown: int = 0
    skip_receiver_ambiguous: int = 0
    skip_method_not_owned: int = 0
    edges_after_dedup: int = 0


def resolve(calls: list[Call], sym: Symbols, source_path: str) -> tuple[list[dict], Stats]:
    stats = Stats()
    edges: list[dict] = []
    for c in calls:
        stats.qualified_candidates += 1
        ids = sym.class_ids_by_label.get(c.receiver)
        if not ids:
            stats.skip_receiver_unknown += 1
            continue
        if len(ids) != 1:
            stats.skip_receiver_ambiguous += 1     # protect precision: never guess
            continue
        cls = next(iter(ids))
        stats.receiver_known += 1
        meth_node = sym.method_id.get((cls, c.method))
        if meth_node is None:
            stats.skip_method_not_owned += 1
            continue
        # caller: enclosing type (if unambiguous) else the file node
        caller = None
        if c.enclosing_type:
            cids = sym.class_ids_by_label.get(c.enclosing_type)
            if cids and len(cids) == 1:
                caller = next(iter(cids))
        if caller is None:
            caller = sym.file_id_by_path.get(source_path)
        if caller is None:
            continue
        stats.resolved += 1
        edges.append({
            "source": caller,
            "target": meth_node,
            "relation": "calls",
            "confidence": "RESOLVED_QUALIFIED",
            "confidence_score": 0.95,
            "weight": 1.0,
            "source_file": source_path,
            "source_location": f"L{c.line}",
            "provenance": PROVENANCE,
        })
    return edges, stats


# ─────────────────────────── driver ───────────────────────────

def run(graph_path: Path, source_root: Path, out_path: Path) -> Stats:
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    sym = build_symbols(graph)

    existing = {(e["source"], e["target"], e.get("relation"))
                for e in (graph.get("edges") or graph.get("links") or [])}
    all_edges: list[dict] = []
    agg = Stats()
    seen: set[tuple[str, str]] = set()

    for swift in sorted(source_root.rglob("*.swift")):
        src = swift.read_bytes()
        calls, nav = extract_calls(src)
        edges, st = resolve(calls, sym, str(swift))
        agg.files += 1
        agg.nav_callees += nav
        for fld in ("qualified_candidates", "receiver_known", "resolved",
                    "skip_receiver_unknown", "skip_receiver_ambiguous", "skip_method_not_owned"):
            setattr(agg, fld, getattr(agg, fld) + getattr(st, fld))
        for e in edges:
            key = (e["source"], e["target"])
            if key in seen or (e["source"], e["target"], "calls") in existing:
                continue
            seen.add(key)
            all_edges.append(e)

    agg.edges_after_dedup = len(all_edges)

    key = "edges" if "edges" in graph else "links"
    graph[key] = (graph.get(key) or []) + all_edges
    out_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    return agg


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Recover qualified Swift call edges for a Graphify graph.")
    ap.add_argument("graph", type=Path, help="path to graphify-out/graph.json")
    ap.add_argument("source_root", type=Path, help="root of the Swift source tree")
    ap.add_argument("-o", "--out", type=Path, default=None, help="output graph.json (default: overwrite input)")
    a = ap.parse_args(argv)  # None -> argparse reads sys.argv[1:]
    out = a.out or a.graph
    st = run(a.graph, a.source_root, out)
    print(json.dumps(st.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
