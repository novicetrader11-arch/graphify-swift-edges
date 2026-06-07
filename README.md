# graphify-swift-edges

Recover **qualified Swift call edges** that [Graphify](https://github.com/safishamsi/graphify)'s
tree-sitter pass records but never *links* — reconnecting cross-type call pathways in the
knowledge graph.

## The problem

Graphify builds a code knowledge graph with tree-sitter. For Swift it correctly records each
type's methods (`Type --method--> .foo()`), but it does **not** link *qualified call sites* back
to the callee's method node:

```swift
// Caller.swift
try PaymentStore.reconcile(id: row.id)          // static, fully resolvable
let rows = PaymentStore.shared.fetch(query: q)   // singleton, fully resolvable
```

These are fully-resolvable calls, yet in the resulting graph the callee type has **zero incoming
`calls` edges**. Types that talk to each other through static or singleton methods become
*disconnected islands*, so `graphify path` and `graphify affected` cannot trace the pathway
between them — even though the call is right there in the source.

## What this tool does

It re-parses the source with the **same** tree-sitter grammar, then resolves qualified calls
**deterministically against the graph's own symbol table** and appends the missing `calls` edges:

1. Build `{TypeLabel → typeNode}` and `{(typeNode, method) → methodNode}` from the graph's
   existing `method` edges.
2. Walk every `call_expression`; keep `navigation_expression` callees of the form
   `Type.method(…)` or `Type.shared.method(…)` (PascalCase receiver).
3. Resolve receiver → type node, attribute the caller by **enclosing-type AST ancestry**
   (not a line heuristic), and emit `caller --calls--> methodNode`.

### Precision guards (why it doesn't invent edges)

An edge is emitted **only** when all hold — otherwise the call is skipped, never guessed:

- the receiver label maps to **exactly one** type node (ambiguous labels are skipped);
- that type **actually owns** a method of that name;
- the receiver is type/singleton-qualified (unqualified `foo()` is skipped — see Scope).

## Validation — [GRDB.swift](https://github.com/groue/GRDB.swift) (479 files)

A large, real-world, open-source Swift codebase (different authors, heavy use of extensions,
generics, and protocols), checkout including its test suite:

| | result |
|---|---|
| `calls` edges added | **70** |
| `path` `PlayerListModel` → `AppDatabase` | "No path found" → **2-hop call edge** |
| precision (independent type-def check) | **69 / 70 (98.6%)** |

Reproduce:

```bash
graphify .                                              # build graphify-out/graph.json on GRDB
graphify-swift-edges graphify-out/graph.json . -o graphify-out/graph.json
graphify path "PlayerListModel" "AppDatabase"          # now a real 2-hop call edge
```

Two honest findings this surfaced:

- **Recall is codebase-dependent, and the guard is deliberately conservative.** GRDB triggered
  **3,614 ambiguous-receiver skips** because extension/generic-heavy types fragment into many
  same-label nodes (`Database` → 15 nodes; `TableRecord`/`FetchRequest` → 5 each) and the test
  suite defines many same-named fixtures (`Player`, `Pet`, `Item`). The tool skips all of these
  rather than guess — fewer edges, never wrong ones. Recovery scales with how many qualified
  calls have an *unambiguous* receiver, which varies a lot by codebase.
- **The single false positive is the documented residual mode.** `Pet.setup()` resolved even
  though GRDB defines `struct Pet` in several test files: the upstream graph had collapsed them
  into one node, so the ambiguity guard couldn't see the collision. 1-in-70, confined to
  duplicated test fixtures — exactly what the IndexStoreDB route (Scope) would eliminate.

Precision was measured **independently** of the parser, via type-definition uniqueness in the raw
source: a receiver whose name has exactly one `class/struct/enum/actor/protocol` definition cannot
be a same-name collision.

## Usage

```bash
uv tool install graphify-swift-edges      # or: pip install graphify-swift-edges
# after `graphify` has built graphify-out/graph.json:
graphify-swift-edges graphify-out/graph.json /path/to/swift/src -o graphify-out/graph.json
```

Then query the reconnected graph with **`graphify path`** (verified working):

```bash
graphify path "SomeCaller" "SomeCallee" --graph graphify-out/graph.json
# Shortest path (2 hops): SomeCaller --calls [RESOLVED_QUALIFIED]--> .method() <--method-- SomeCallee
```

**`affected` caveat (measured):** recovered edges point at *method* nodes. `graphify affected`
on the owning **type** returns nothing (it reverse-traverses `calls` but not `method`), and on a
**method** label it errors with `No unique node match` (labels like `.fetch()` are ambiguous
across types). So use `path` for cross-type tracing, not `affected`, until Graphify's `affected`
learns to hop `method` edges.

## Scope (honest boundaries)

- **Recovers:** type-qualified (`Foo.bar()`) and singleton-qualified (`Foo.shared.bar()`) calls.
- **Does NOT recover:** instance-variable calls (`let s = Foo(); s.bar()`) — these need real
  type inference. The correct general fix is a **SourceKit / IndexStoreDB** extractor that reads
  Swift's precise semantic index (protocol dispatch, generics, inferred types included). That is
  the documented sequel, not this tool.
- The residual false-positive mode (a same-name type whose duplicate definitions the upstream
  graph collapsed into one node) is rare and, in testing, confined to duplicated test fixtures.
- Validated on one large open-source codebase; the resolution is language-general in principle
  (Kotlin, TS have the same qualified-call shape) but only Swift is implemented and measured here.

## Tests

```bash
uv run --with pytest --with tree-sitter --with tree-sitter-swift python -m pytest
```

## License

MIT
