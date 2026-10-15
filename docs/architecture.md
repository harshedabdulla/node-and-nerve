# Architecture

```
            HTTP clients (agents, scripts, UIs — any language)
                               │ JSON
┌──────────────────────────────▼───────────────────────────────┐
│ api.py      FastAPI routes, request validation, error mapping │
│ auth.py     API keys scoped to companies                      │
│ models.py   Pydantic request/response models, schema checks   │
├───────────────────────────────────────────────────────────────┤
│ investigate.py  deterministic investigation                   │
│ ingest.py       idempotent writes and change events           │
│ lookup.py       entity view, decision provenance, connections │
├───────────────────────────────────────────────────────────────┤
│ queries.py  named Cypher templates     schema.py  model rules │
├───────────────────────────────────────────────────────────────┤
│ graph.py    GraphStore protocol, FalkorDBStore adapter        │
└──────────────────────────────┬───────────────────────────────┘
                               │ RESP (falkordb-py)
                    FalkorDB — one graph per company
```

There is no other datastore. FalkorDB is the system of record for the context graph.

## Request flow: an investigation

1. `api.py` validates the body and checks the API key against the company in the path.
2. `Investigator.investigate` confirms the company graph exists (`404` otherwise).
3. It resolves the focus: given `focus_ids`, or by matching question words against entity names and ids (`resolve_entities`), preferring projects, then components, then tickets.
4. It runs the fixed query sequence (`tickets_in_scope` → `ticket_status` → `ticket_incident_paths` → `incidents` → `decisions_affecting` + `current_decisions` → `experts_for_components` → `current_owners` → `sources_for`). Each step's parameters come from the previous step's rows.
5. It renders findings from rows with fixed templates, builds evidence paths from returned edges, and records what was expected but absent (`missing_information`) or questionable (`warnings`).
6. The response lists `queries_executed`, so a caller can see which graph queries produced the answer.

There is no LLM call and no randomness; the same graph and request give the same result (apart from the generated `investigation_id`).

## Writes

`Ingestor` validates kinds and endpoints in Pydantic before any query, checks referenced entities exist, then MERGEs. Events are composed from the same primitives. Writes are idempotent by construction, so a connector can replay a batch after a failure without deduplication logic of its own.

## Error handling

| Condition | HTTP |
| --- | --- |
| Invalid body, id or company id | 422 |
| Unknown or unauthorized API key / company | 403 (same response for both) |
| Company graph or entity does not exist | 404 |
| Unique-constraint violation | 409 |
| FalkorDB unreachable | 503 |
| Query exceeded its timeout | 504 |
| Any other database error | 500 with a generic message (details only in server logs) |

`/health` never touches the database. `/ready` runs `PING` and `GRAPH.LIST`. The FalkorDB client connects lazily, so the process starts and reports "not ready" while the database is down.

## Testing seams

- `GraphStore` is a `Protocol`. Unit tests use `tests/conftest.py::FakeStore`, which returns canned rows and records which named queries ran with which parameters.
- Integration tests (`-m integration`) run every query, the seed, events, tenancy and the HTTP API against a real FalkorDB, each in a throwaway company graph.
- `tests/test_queries.py` statically checks all templates: read queries contain no write clauses, labels/types come from the schema, traversals are bounded, nothing is left unrendered.

## Design choices

- **Graph per tenant** — see [graph-model.md](graph-model.md#tenancy).
- **Templates over a query builder** — a reviewer can read every query the service can run in one file.
- **Events are synchronous HTTP calls**, not a bus. A connector posts them; there is no queue to operate.
- **Rendered statements, not generated text** — the service's output is meant to be checked, not trusted.
