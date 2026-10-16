# Cypher queries

All queries live in [`src/company_brain/queries.py`](../src/company_brain/queries.py) as named `Query` objects. The store executes only these objects, never strings. Read queries run through `GRAPH.RO_QUERY`, so FalkorDB itself rejects any write in them.

Rules:

- Caller values are always `$parameters`.
- Labels and relationship types cannot be parameters in FalkorDB, so they are rendered from the `Kind` and `Rel` enums when the module loads. The set of query strings is finite (`queries.all_queries()`), and tests check every one of them.
- Variable-length patterns have literal upper bounds (`*0..2`, `*1..4`, `*1..5`). Path and lookup queries have `LIMIT`s.
- Current ownership means `OWNS` edges with `valid_to IS NULL`.

Tested against FalkorDB **v4.20.7** (`tests/test_integration_graph.py`).

## Writes

### Upsert entity (one template per kind)

```cypher
MERGE (n:Entity:Ticket {id: $id})
ON CREATE SET n.kind = $kind, n.created_at = $ts
WITH n WHERE n.updated_at IS NULL OR n.updated_at <= $ts
SET n += $props, n.updated_at = $ts
RETURN n.id AS id
```

MERGE on the unique `id` makes the write idempotent. The `WITH … WHERE` guard drops writes older than what is stored (timestamps are fixed-format UTC strings, so they compare lexically). No row back means "ignored as stale".

### Upsert relationship (one template per type)

```cypher
MATCH (a:Entity {id: $from_id}), (b:Entity {id: $to_id})
MERGE (a)-[r:BLOCKED_BY]->(b)
ON CREATE SET r.created_at = $ts, r.valid_from = $ts
SET r += $props, r.updated_at = $ts, r.valid_to = NULL
RETURN type(r) AS type, a.id AS from_id, b.id AS to_id
```

`$props` may include `source_id`, the source record that asserted the edge.

### Ownership change

```cypher
MATCH (old:Entity)-[o:OWNS]->(t:Entity {id: $entity_id})
WHERE o.valid_to IS NULL AND old.kind = $owner_kind AND old.id <> $owner_id
SET o.valid_to = $ts, o.closed_by_event = $event_id
RETURN old.id AS previous_owner_id
```

followed by an `OWNS` upsert for the new owner. Only owners of the same kind are closed, so a person reassignment leaves team ownership alone. Old edges are kept as history.

Other writes: `set_ticket_status` (guarded by `updated_at`), `withdraw_source`.

## Reads used by investigations

### 1. A project's tickets, and which are blocked or overdue

```cypher
MATCH (scope:Entity {id: $scope_id})<-[r:PART_OF|IN_COMPONENT]-(t:Ticket)
RETURN t.id AS id, type(r) AS via ORDER BY id LIMIT 200
```

```cypher
UNWIND $ticket_ids AS tid
MATCH (t:Ticket {id: tid})
OPTIONAL MATCH (t)-[b:BLOCKED_BY]->(blocker:Entity)
WHERE NOT coalesce(blocker.status, 'open') IN $closed
OPTIONAL MATCH (owner:Entity)-[o:OWNS]->(t)
WHERE o.valid_to IS NULL
WITH t,
     collect(DISTINCT CASE WHEN blocker IS NULL THEN NULL ELSE {id: blocker.id, …, edge_source_id: b.source_id} END) AS blockers,
     collect(DISTINCT CASE WHEN owner IS NULL THEN NULL ELSE {id: owner.id, …} END) AS owners
RETURN properties(t) AS ticket, blockers, owners,
       [(t)-[:IN_COMPONENT]->(c:Component) | c.id] AS components,
       [(t)-[:PART_OF]->(p:Project) | p.id] AS projects,
       (t.due_date IS NOT NULL AND t.due_date < $as_of AND NOT t.status IN ['done', 'cancelled']) AS overdue
```

A blocker that is done, cancelled or resolved no longer counts.

### 2. Tickets to incidents, through the open blocker chain (`ticket_incident_paths`)

```cypher
UNWIND $ticket_ids AS tid
MATCH p = (t:Ticket {id: tid})-[:BLOCKED_BY*0..2]->(x:Entity)<-[:AFFECTS]-(i:Incident)
WHERE ALL(n IN nodes(p) WHERE n.kind <> 'ticket' OR n.id = tid OR NOT n.status IN $closed)
RETURN tid AS ticket_id, i.id AS incident_id, 'affects_blocker_chain' AS relation,
       [r IN relationships(p) | {type: type(r), from: startNode(r).id, to: endNode(r).id, source_id: r.source_id}] AS rels
UNION
UNWIND $ticket_ids AS tid
MATCH p = (t:Ticket {id: tid})-[:BLOCKED_BY*0..2]->(x:Entity)-[:IN_COMPONENT]->(c:Component)<-[:IN_COMPONENT]-(i:Incident)
WHERE ALL(n IN nodes(p) WHERE n.kind <> 'ticket' OR n.id = tid OR NOT n.status IN $closed)
RETURN tid AS ticket_id, i.id AS incident_id, 'same_component' AS relation, [...] AS rels
```

`*0..2` includes incidents that affect the ticket directly. The second half finds similar incidents in the same component (e.g. INC-007, resolved by Lena Ortiz). Every row carries its full path, which becomes an `evidence_path` in the response.

### 3. Sources: supports vs. mentions vs. contradicts (`sources_for`)

```cypher
UNWIND $ids AS eid
MATCH (e:Entity {id: eid})-[r:SUPPORTED_BY]->(s:Source)
RETURN eid AS entity_id, 'supports' AS relation, {id: s.id, uri: s.uri, status: coalesce(s.status, 'active'), …,
       authors: [(s)-[:AUTHORED_BY]->(au:Person) | au.id]} AS source
UNION ALL
UNWIND $ids AS eid
MATCH (s:Source)-[r:MENTIONS|CONTRADICTS]->(e:Entity {id: eid})
RETURN eid AS entity_id, CASE type(r) WHEN 'MENTIONS' THEN 'mentions' ELSE 'contradicts' END AS relation, {…} AS source
```

Withdrawn sources are still returned (with `status: withdrawn`) so the caller can see them, but they do not make a finding `supported`.

### 4. Current ownership (`current_owners`)

```cypher
UNWIND $ids AS eid
MATCH (owner:Entity)-[o:OWNS]->(x:Entity {id: eid})
WHERE o.valid_to IS NULL
RETURN eid AS entity_id, owner.id AS owner_id, owner.kind AS owner_kind, owner.name AS owner_name,
       o.source_id AS source_id, [(owner)-[:MEMBER_OF]->(tm:Team) | tm.id] AS owner_teams
```

### 5. Expertise from recorded relationships (`experts_for_components`)

Four `UNION ALL` branches, one per kind of evidence:

```cypher
MATCH (p:Person)-[:RESOLVED]->(i:Incident)-[:IN_COMPONENT]->(c:Component {id: cid})        -- resolved_incident
MATCH (p:Person)-[o:OWNS]->(c:Component {id: cid}) WHERE o.valid_to IS NULL               -- owns_component
MATCH (p:Person)-[:MEMBER_OF]->(t:Team)-[o:OWNS]->(c:Component {id: cid}) WHERE o.valid_to IS NULL  -- member_of_owning_team
MATCH (s:Source)-[:AUTHORED_BY]->(p:Person), (i:Incident)-[:IN_COMPONENT]->(c:Component {id: cid}),
      (i)-[:SUPPORTED_BY]->(s) WHERE coalesce(s.status, 'active') = 'active'              -- authored_incident_source
```

The service sums fixed weights per person and returns the basis for each point. Nothing is inferred beyond these edges.

### 6. Decisions affecting the scope (`decisions_affecting`)

```cypher
UNWIND $target_ids AS tid
MATCH (d:Decision)-[:AFFECTS]->(x:Entity {id: tid})
WITH d, collect(DISTINCT x.id) AS affects
RETURN properties(d) AS decision, affects,
       [(newer:Decision)-[:SUPERSEDES]->(d) WHERE coalesce(newer.status, 'accepted') = 'accepted' | newer.id] AS superseded_by,
       [(d)-[:SUPERSEDES]->(older:Decision) | older.id] AS supersedes,
       [(d)-[:DECIDED_BY]->(p:Person) | {id: p.id, name: p.name}] AS decided_by
ORDER BY d.decided_at DESC, d.id LIMIT 50
```

### 7. Decision provenance (`decision_provenance`)

```cypher
MATCH (d:Decision {id: $id})
OPTIONAL MATCH chain = (d)-[:SUPERSEDES*1..5]->(older:Decision)
WITH d, collect(DISTINCT [n IN nodes(chain) | n.id]) AS supersedes_chains
RETURN properties(d) AS decision, …,
       [(d)-[:AFFECTS]->(:Component)<-[:IN_COMPONENT]-(w:Entity) WHERE w.kind IN ['ticket', 'incident'] | w.id] AS related_work,
       [(d)<-[:MENTIONS]-(:Source)-[:MENTIONS]->(w:Entity) WHERE w.kind IN ['ticket', 'incident'] | w.id] AS co_mentioned_work
```

Combined with `sources_for`, `GET /decisions/{id}/sources` returns supporting, mentioning and contradicting sources separately.

### 8. Current guidance excludes superseded decisions (`current_decisions`)

```cypher
UNWIND $target_ids AS tid
MATCH (d:Decision)-[:AFFECTS]->(x:Entity {id: tid})
WHERE coalesce(d.status, 'accepted') = 'accepted'
WITH DISTINCT d
OPTIONAL MATCH (newer:Decision)-[:SUPERSEDES]->(d)
WHERE coalesce(newer.status, 'accepted') = 'accepted'
WITH d, count(newer) AS superseders
WHERE superseders = 0
RETURN d.id AS id ORDER BY id
```

The exclusion happens in the graph, based on `SUPERSEDES` edges, not on a status flag someone has to remember to update.

### 9. Bounded connection discovery (`connections_depth_1` … `connections_depth_4`)

```cypher
MATCH (a:Entity {id: $from_id}), (b:Entity {id: $to_id})
MATCH p = (a)-[*1..3]-(b)
WHERE ALL(r IN relationships(p) WHERE r.valid_to IS NULL)
RETURN [n IN nodes(p) | {id: n.id, kind: n.kind, name: n.name}] AS nodes,
       [r IN relationships(p) | {type: type(r), from: startNode(r).id, to: endNode(r).id, source_id: r.source_id}] AS rels,
       length(p) AS hops
ORDER BY hops LIMIT $limit
```

One template per depth, because the bound must be a literal. Closed (historical) ownership edges are not traversed.

### Evidence chain assembly

The Project → Ticket → Incident → Source → Decision → Person chain is assembled from the rows above, not invented: the incident path (query 2), the incident's supporting source (query 3), that source's `MENTIONS` edge to a current decision (queries 3 and 8), and the decision's `DECIDED_BY` edge (query 6). Each edge in the output corresponds to an edge returned by FalkorDB.

## Cashflow Radar reads

`cf_receivables`, `cf_commitments_due`, `cf_dependency_paths` and `cf_sources_by_id` are described, with their Cypher, in [cashflow-radar.md](cashflow-radar.md#how-falkordb-does-the-work). Two dialect points they rely on: `sum()` returns a float, so balances are wrapped in `toInteger()`; and named paths in `OPTIONAL MATCH` are tested with `CASE WHEN p IS NULL THEN [] ELSE … END` before calling `relationships(p)`.

## Contract costing reads

The `mf_*` templates (contract, charges, category totals, noted-but-unsplit costs, unallocated costs, unapplied payments, overview, changes since) are described with their Cypher in [contract-costing.md](contract-costing.md#how-falkordb-does-the-work). Dialect point found while building them: reusing a variable name across pattern comprehensions in one `RETURN` made later comprehensions come back empty, so each comprehension uses its own variable names.

## FalkorDB dialect notes (v4.20.7)

Found while building this; each one shaped a query above.

| Behaviour | Consequence here |
| --- | --- |
| Labels and relationship types cannot be parameters (`SET n:$label` is a syntax error) | Templates are rendered per enum value at import time |
| Variable-length bounds cannot be parameters (`*1..$d`) | One `connections_depth_N` template per depth |
| `shortestPath` is only allowed in `WITH`/`RETURN`, and only for directed patterns | Undirected connection discovery uses bounded enumeration + `ORDER BY length(p) LIMIT` |
| No `EXISTS { … }` subqueries; pattern comprehensions are not allowed in `WHERE` | Supersession is counted with `OPTIONAL MATCH … count()` |
| No `datetime()` function | Timestamps are passed in as ISO-8601 UTC strings |
| A unique constraint needs a range index on the same property first | `ensure_schema` creates the index, then the constraint |
| `GRAPH.RO_QUERY` on a graph that does not exist fails with "empty key" | Mapped to `404 company has no graph yet`; reads never create graphs |
| Setting a property to `NULL` removes it | Used to reopen ownership (`r.valid_to = NULL`) |
| Unreferenced relationship aliases in a pattern only check existence (documented limitation) | Queries reference the aliases they rely on |
