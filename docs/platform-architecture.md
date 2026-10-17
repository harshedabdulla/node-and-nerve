# Node & Nerve platform: current architecture and the path to workspaces

Status: 17 Oct 2026. Phase 1 is done; phases 2 to 6 are planned, not built.

## What runs today

```
browser ──> web/ (Next.js 15, Vercel bom1) ──server only──> API (FastAPI, Vercel bom1) ──> FalkorDB Cloud (AWS ap-south-1)
                                                              ^
Telegram ──webhook (secret header)───────────────────────────┘
```

| Part | Where | Notes |
| --- | --- | --- |
| Site | `web/`, https://node-and-nerve.vercel.app | Landing page and `/demo`. Server components call the API with a key that is never sent to the browser. Demo pages read only the synthetic tenants `kaveri-body-works` and `northstar`. |
| API | `src/company_brain`, `api/index.py`, https://node-and-nerve-api.vercel.app | Ingest (entities, relationships, sources, events, CSV), three investigations (Atlas, Cashflow Radar, contract costing), trace pages, Telegram webhook. |
| Graph | FalkorDB Cloud | One graph per company (`company_brain__<id>`), the isolation boundary. |
| Auth | `COMPANY_BRAIN_API_KEYS` | Static keys scoped to companies. There are no users, sessions or workspaces yet. |
| Bot | `telegram_bot.py` | One bot, one company (`AGENT_COMPANY_ID`), one allow-list. |

What is already company-agnostic: tenant-per-graph isolation and company id validation; the ingest layer (MERGE on stable ids, older data never overwrites newer, `SUPPORTED_BY` provenance, CSV validate-then-write); typed relationship rules; fixed, parameterized query templates with an executed-query log; the finding model (`fact`, `calculation`, `unknown`, `inconsistency`, `decision`) with evidence paths; signed trace links; the agent's tool loop.

What is tied to one demo: the bot's single company and contract-only commands; the site's demo pages (by design); the fixed reporting date `2026-10-16` used by the demos; and each investigation, which knows one domain (engineering delivery, receivables, contract costing). There is no investigation that works over whatever a workspace has connected.

## Target boundaries

| Concern | Owner | Store |
| --- | --- | --- |
| Identity, sign-up, sign-in, recovery, sessions | Clerk (in `web/`) | Clerk |
| Workspaces, membership, roles | Clerk Organizations, mirrored in the control plane | Clerk + Postgres |
| Control plane: workspace profile, onboarding state, connections, sync runs, notification rules, deliveries, investigation history, audit log | `web/` server routes | Neon Postgres |
| Connector credentials and OAuth | Composio (`user_id` = workspace id) | Composio; we store only connection ids and status |
| Ingestion and normalization | API: one module per connector, writing through the existing `Ingestor` | FalkorDB, one graph per workspace |
| Graph traversal and investigations | API | FalkorDB |
| Notification delivery | API job triggered by a scheduler; Telegram first | Postgres (rules, deliveries) |

Decisions and trade-offs:

- **Clerk over self-hosted auth.** Account recovery, sessions and organisation membership come built in, which removes the riskiest code. The cost is a vendor dependency. Organisation ids map one-to-one to workspaces.
- **Postgres for the control plane, FalkorDB for company knowledge.** Accounts, connections and notification rules are relational records with uniqueness and audit needs. Company knowledge stays in the graph, and investigations keep traversing it. Postgres never answers a business question.
- **The browser never calls the API directly.** `web/` server code checks the Clerk session, checks membership of the requested workspace in Postgres, then calls the API with a service key. The workspace id in the request is taken from the verified membership, never from the client alone.
- **Workspace graphs are named from an opaque id** (`ws-<random>`), never from a company name, and can never equal a demo tenant id. Demo tenants stay readable only through the demo pages.
- **Composio is the connector layer, but each connector declares what it verifiably supports.** A connection is shown as connected only after Composio reports it active *and* a first read succeeded.

## Connector contract

Each connector is a small module in the API:

```python
class Connector(Protocol):
    key: str                         # "google_sheets"
    toolkit: str                     # Composio toolkit slug
    reads: list[ReadSpec]            # tool slug + normalizer, validated against live discovery
    def normalize(self, raw: dict) -> NormalizedBatch: ...  # entities, relationships, sources
```

`NormalizedBatch` goes through the existing `Ingestor`, so idempotency, version ordering and provenance come for free. Every record carries `source_system`, `source_record_id` and `observed_at`. Raw third-party content is never passed to a model as instructions; it is stored as record properties and source excerpts only.

Verified status (from the Spindle Composio gateway, early October 2026; to be re-verified with the Node & Nerve Composio project before building):

| Integration | Route | Reads | Notes |
| --- | --- | --- | --- |
| Telegram | Native bot (exists) | Messages to the bot | Notification channel first. Composio is not needed. |
| Google Sheets | Composio `googlesheets`, OAuth | `GOOGLESHEETS_BATCH_GET` | First ingestion connector. The user picks a sheet and maps columns. |
| Zoho Books / Inventory | Composio `zoho_books`, `zoho_inventory` | None | The account connects, but Composio exposes zero tools for these toolkits. Shown as "waiting on connector". |

Sheets has no push events through Composio, so syncing is scheduled polling: one run per connection on a fixed interval, skipped if the previous run is still going, with a hash per row so unchanged rows write nothing.

## Investigations over any workspace

The first general investigation is **"what needs attention"**: blocked or overdue work, dependency chains, items without an owner, decisions that superseded others, and records without a source. It reuses the Atlas traversal patterns (`BLOCKED_BY`, `OWNS`, `SUPERSEDES`, `SUPPORTED_BY`) over the core kinds (person, team, project, ticket/task, decision, source). Domain investigations (contract costing, cashflow) stay available when a workspace's graph holds those kinds.

## Notifications

A rule names a workspace, a check (an investigation plus a condition, for example "new blocked task" or "unsplit cost over ₹10,000"), a channel and a schedule. The scheduler runs due rules; each result has a fingerprint, and a delivery is recorded before sending so the same finding is never sent twice. Anything that would change an external system is proposed and waits for approval; nothing is sent to customers or suppliers.

## Phases

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 1. Stabilize (done) | Validation fixes, cloud deployment, monorepo, redesigned landing, separate `/demo` | CT-104 figures match an independent oracle on FalkorDB Cloud; site and bot run on it |
| 2. Foundation | Clerk sign-in, workspaces, Neon control plane, app shell at `/app`, service-key calls with membership checks | A user can sign up, create a workspace and see an empty state; a second user cannot read it (tested) |
| 3. First integration | Composio Google Sheets connect, column mapping, sync runs with status and errors, records in the workspace graph | A real sheet's rows appear as graph nodes with sources; re-sync writes nothing; disconnect works |
| 4. Onboarding | Short guided setup: name, what to watch, sources, channel | Completion is shown only after the first sync is verified in the graph |
| 5. Investigation and alerts | "What needs attention" over any workspace; Telegram linked per workspace; rules and deliveries | An alert fires once for a new blocked task and not again |
| 6. Hardening | Isolation, failure, reconnect, idempotency and duplicate-delivery tests; docs | All pass in CI against a real FalkorDB |

## What is needed to start phase 2

- A Clerk application (publishable and secret keys), with Organizations enabled.
- A Neon database added to the `node-and-nerve` Vercel project.
- A Composio project for Node & Nerve (`COMPOSIO_API_KEY`), needed in phase 3.
