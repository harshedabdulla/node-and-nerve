"""Named, fixed Cypher templates.

Rules every template follows:
  * Caller values are passed as ``$parameters``, never interpolated.
  * Labels and relationship types are rendered only from the ``Kind``/``Rel`` enums,
    because FalkorDB does not accept them as parameters. The full set of query
    strings is therefore finite and enumerable (see ``all_queries``).
  * Traversals have literal upper bounds and results have limits.
  * Ownership edges carry ``valid_to`` once superseded; current owners are
    those with ``valid_to IS NULL``.

Verified against FalkorDB v4.20.7. Dialect notes are in docs/cypher.md.
"""

from __future__ import annotations

from company_brain.graph import Query
from company_brain.schema import Kind, Rel

MAX_PATH_DEPTH = 4

# Statuses that mean "this no longer blocks anything".
CLOSED_STATUSES = ["done", "cancelled", "resolved"]

_SOURCE_FIELDS = (
    "{id: s.id, name: s.name, uri: s.uri, source_type: s.source_type, "
    "status: coalesce(s.status, 'active'), excerpt: s.excerpt, "
    "source_system: s.source_system, source_record_id: s.source_record_id, "
    "authors: [(s)-[:AUTHORED_BY]->(au:Person) | au.id]}"
)

_REL_FIELDS = "{type: type(r), from: startNode(r).id, to: endNode(r).id, source_id: r.source_id}"

# ---------------------------------------------------------------- writes

UPSERT_ENTITY: dict[Kind, Query] = {
    kind: Query(
        name=f"upsert_entity_{kind.value}",
        read_only=False,
        cypher=f"""
MERGE (n:Entity:{kind.label} {{id: $id}})
ON CREATE SET n.kind = $kind, n.created_at = $ts
WITH n WHERE n.updated_at IS NULL OR n.updated_at <= $ts
SET n += $props, n.updated_at = $ts
RETURN n.id AS id""",
    )
    for kind in Kind
}

UPSERT_RELATIONSHIP: dict[Rel, Query] = {
    rel: Query(
        name=f"upsert_relationship_{rel.value.lower()}",
        read_only=False,
        cypher=f"""
MATCH (a:Entity {{id: $from_id}}), (b:Entity {{id: $to_id}})
MERGE (a)-[r:{rel.value}]->(b)
ON CREATE SET r.created_at = $ts, r.valid_from = $ts
SET r += $props, r.updated_at = $ts, r.valid_to = NULL
RETURN type(r) AS type, a.id AS from_id, b.id AS to_id""",
    )
    for rel in Rel
}

CLOSE_OTHER_OWNERS = Query(
    name="close_other_owners",
    read_only=False,
    cypher="""
MATCH (old:Entity)-[o:OWNS]->(t:Entity {id: $entity_id})
WHERE o.valid_to IS NULL AND old.kind = $owner_kind AND old.id <> $owner_id
SET o.valid_to = $ts, o.closed_by_event = $event_id
RETURN old.id AS previous_owner_id""",
)

SET_TICKET_STATUS = Query(
    name="set_ticket_status",
    read_only=False,
    cypher="""
MATCH (t:Ticket {id: $id})
WITH t, t.status AS previous
WHERE t.updated_at IS NULL OR t.updated_at <= $ts
SET t.status = $status, t.updated_at = $ts, t.status_changed_at = $ts
RETURN t.id AS id, previous AS previous_status""",
)

WITHDRAW_SOURCE = Query(
    name="withdraw_source",
    read_only=False,
    cypher="""
MATCH (s:Source {id: $id})
SET s.status = 'withdrawn', s.withdrawn_reason = $reason, s.updated_at = $ts
RETURN s.id AS id""",
)

# ---------------------------------------------------------------- reads

ENTITIES_EXIST = Query(
    name="entities_exist",
    cypher="""
UNWIND $ids AS eid
MATCH (n:Entity {id: eid})
RETURN n.id AS id, n.kind AS kind""",
)

GET_ENTITY = Query(
    name="get_entity",
    cypher="""
MATCH (n:Entity {id: $id})
OPTIONAL MATCH (n)-[r]-(m:Entity)
WITH n, r, m ORDER BY type(r), m.id LIMIT 200
RETURN properties(n) AS entity,
       collect(CASE WHEN r IS NULL THEN NULL ELSE {
           type: type(r),
           direction: CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END,
           other_id: m.id, other_kind: m.kind, other_name: m.name,
           current: r.valid_to IS NULL,
           properties: properties(r)} END) AS relationships""",
)

RESOLVE_ENTITIES = Query(
    name="resolve_entities",
    cypher="""
UNWIND $terms AS term
MATCH (n:Entity)
WHERE n.kind IN $kinds AND (toLower(n.name) CONTAINS term OR n.id ENDS WITH term)
RETURN n.id AS id, n.kind AS kind, n.name AS name, collect(DISTINCT term) AS matched_terms
ORDER BY size(matched_terms) DESC, id
LIMIT 20""",
)

# Q1 (scope): tickets that belong to a project or component.
TICKETS_IN_SCOPE = Query(
    name="tickets_in_scope",
    cypher="""
MATCH (scope:Entity {id: $scope_id})<-[r:PART_OF|IN_COMPONENT]-(t:Ticket)
RETURN t.id AS id, type(r) AS via
ORDER BY id
LIMIT 200""",
)

# Q1 + Q4: status, overdue flag, open blockers and current owners of each ticket.
TICKET_STATUS = Query(
    name="ticket_status",
    cypher="""
UNWIND $ticket_ids AS tid
MATCH (t:Ticket {id: tid})
OPTIONAL MATCH (t)-[b:BLOCKED_BY]->(blocker:Entity)
WHERE NOT coalesce(blocker.status, 'open') IN $closed
OPTIONAL MATCH (owner:Entity)-[o:OWNS]->(t)
WHERE o.valid_to IS NULL
WITH t,
     collect(DISTINCT CASE WHEN blocker IS NULL THEN NULL ELSE {
         id: blocker.id, kind: blocker.kind, name: blocker.name,
         status: blocker.status, edge_source_id: b.source_id} END) AS blockers,
     collect(DISTINCT CASE WHEN owner IS NULL THEN NULL ELSE {
         id: owner.id, kind: owner.kind, name: owner.name} END) AS owners
RETURN properties(t) AS ticket, blockers, owners,
       [(t)-[:IN_COMPONENT]->(c:Component) | c.id] AS components,
       [(t)-[:PART_OF]->(p:Project) | p.id] AS projects,
       (t.due_date IS NOT NULL AND t.due_date < $as_of
        AND NOT t.status IN ['done', 'cancelled']) AS overdue
ORDER BY t.id""",
)

# Q4: current owners of any entity, plus the team each person owner belongs to.
CURRENT_OWNERS = Query(
    name="current_owners",
    cypher="""
UNWIND $ids AS eid
MATCH (owner:Entity)-[o:OWNS]->(x:Entity {id: eid})
WHERE o.valid_to IS NULL
RETURN eid AS entity_id, owner.id AS owner_id, owner.kind AS owner_kind,
       owner.name AS owner_name, o.source_id AS source_id,
       [(owner)-[:MEMBER_OF]->(tm:Team) | tm.id] AS owner_teams
ORDER BY entity_id, owner_id""",
)

# Q2 + Q9: incidents reachable from a ticket through its open blocker chain
# (bounded to two hops), returning the full path so callers can display it.
TICKET_INCIDENT_PATHS = Query(
    name="ticket_incident_paths",
    cypher=f"""
UNWIND $ticket_ids AS tid
MATCH p = (t:Ticket {{id: tid}})-[:BLOCKED_BY*0..2]->(x:Entity)<-[:AFFECTS]-(i:Incident)
WHERE ALL(n IN nodes(p) WHERE n.kind <> 'ticket' OR n.id = tid OR NOT n.status IN $closed)
RETURN tid AS ticket_id, i.id AS incident_id, 'affects_blocker_chain' AS relation,
       [r IN relationships(p) | {_REL_FIELDS}] AS rels
UNION
UNWIND $ticket_ids AS tid
MATCH p = (t:Ticket {{id: tid}})-[:BLOCKED_BY*0..2]->(x:Entity)
          -[:IN_COMPONENT]->(c:Component)<-[:IN_COMPONENT]-(i:Incident)
WHERE ALL(n IN nodes(p) WHERE n.kind <> 'ticket' OR n.id = tid OR NOT n.status IN $closed)
RETURN tid AS ticket_id, i.id AS incident_id, 'same_component' AS relation,
       [r IN relationships(p) | {_REL_FIELDS}] AS rels""",
)

INCIDENTS = Query(
    name="incidents",
    cypher="""
UNWIND $ids AS iid
MATCH (i:Incident {id: iid})
RETURN properties(i) AS incident,
       [(i)-[:IN_COMPONENT]->(c:Component) | c.id] AS components,
       [(i)-[:AFFECTS]->(x:Entity) | x.id] AS affects,
       [(p:Person)-[:RESOLVED]->(i) | p.id] AS resolved_by
ORDER BY i.id""",
)

# Q3 + Q7: sources attached to entities, distinguishing support from mention
# and contradiction.
SOURCES_FOR = Query(
    name="sources_for",
    cypher=f"""
UNWIND $ids AS eid
MATCH (e:Entity {{id: eid}})-[r:SUPPORTED_BY]->(s:Source)
RETURN eid AS entity_id, 'supports' AS relation, {_SOURCE_FIELDS} AS source
UNION ALL
UNWIND $ids AS eid
MATCH (s:Source)-[r:MENTIONS|CONTRADICTS]->(e:Entity {{id: eid}})
RETURN eid AS entity_id,
       CASE type(r) WHEN 'MENTIONS' THEN 'mentions' ELSE 'contradicts' END AS relation,
       {_SOURCE_FIELDS} AS source""",
)

# Q5: expertise inferred only from recorded relationships.
EXPERTS_FOR_COMPONENTS = Query(
    name="experts_for_components",
    cypher="""
UNWIND $component_ids AS cid
MATCH (p:Person)-[:RESOLVED]->(i:Incident)-[:IN_COMPONENT]->(c:Component {id: cid})
RETURN p.id AS person_id, p.name AS name, cid AS component_id,
       'resolved_incident' AS basis, i.id AS evidence_id
UNION ALL
UNWIND $component_ids AS cid
MATCH (p:Person)-[o:OWNS]->(c:Component {id: cid})
WHERE o.valid_to IS NULL
RETURN p.id AS person_id, p.name AS name, cid AS component_id,
       'owns_component' AS basis, c.id AS evidence_id
UNION ALL
UNWIND $component_ids AS cid
MATCH (p:Person)-[:MEMBER_OF]->(t:Team)-[o:OWNS]->(c:Component {id: cid})
WHERE o.valid_to IS NULL
RETURN p.id AS person_id, p.name AS name, cid AS component_id,
       'member_of_owning_team' AS basis, t.id AS evidence_id
UNION ALL
UNWIND $component_ids AS cid
MATCH (s:Source)-[:AUTHORED_BY]->(p:Person),
      (i:Incident)-[:IN_COMPONENT]->(c:Component {id: cid}), (i)-[:SUPPORTED_BY]->(s)
WHERE coalesce(s.status, 'active') = 'active'
RETURN p.id AS person_id, p.name AS name, cid AS component_id,
       'authored_incident_source' AS basis, s.id AS evidence_id""",
)

# Q6: every decision that affects the targets, with its supersession links.
DECISIONS_AFFECTING = Query(
    name="decisions_affecting",
    cypher="""
UNWIND $target_ids AS tid
MATCH (d:Decision)-[:AFFECTS]->(x:Entity {id: tid})
WITH d, collect(DISTINCT x.id) AS affects
RETURN properties(d) AS decision, affects,
       [(newer:Decision)-[:SUPERSEDES]->(d)
         WHERE coalesce(newer.status, 'accepted') = 'accepted' | newer.id] AS superseded_by,
       [(d)-[:SUPERSEDES]->(older:Decision) | older.id] AS supersedes,
       [(d)-[:DECIDED_BY]->(p:Person) | {id: p.id, name: p.name}] AS decided_by
ORDER BY d.decided_at DESC, d.id
LIMIT 50""",
)

# Q8: current guidance. The graph itself excludes decisions that an accepted
# decision supersedes. (FalkorDB v4.20 has no EXISTS {} subqueries and no
# pattern comprehensions in WHERE, so supersession is counted with OPTIONAL MATCH.)
CURRENT_DECISIONS = Query(
    name="current_decisions",
    cypher="""
UNWIND $target_ids AS tid
MATCH (d:Decision)-[:AFFECTS]->(x:Entity {id: tid})
WHERE coalesce(d.status, 'accepted') = 'accepted'
WITH DISTINCT d
OPTIONAL MATCH (newer:Decision)-[:SUPERSEDES]->(d)
WHERE coalesce(newer.status, 'accepted') = 'accepted'
WITH d, count(newer) AS superseders
WHERE superseders = 0
RETURN d.id AS id
ORDER BY id""",
)

# Q7: full provenance for one decision.
DECISION_PROVENANCE = Query(
    name="decision_provenance",
    cypher="""
MATCH (d:Decision {id: $id})
OPTIONAL MATCH chain = (d)-[:SUPERSEDES*1..5]->(older:Decision)
WITH d, collect(DISTINCT [n IN nodes(chain) | n.id]) AS supersedes_chains
RETURN properties(d) AS decision,
       [(d)-[:DECIDED_BY]->(p:Person) | {id: p.id, name: p.name}] AS decided_by,
       [(d)-[:AFFECTS]->(x:Entity) | {id: x.id, kind: x.kind, name: x.name}] AS affects,
       [(newer:Decision)-[:SUPERSEDES]->(d) | newer.id] AS superseded_by,
       supersedes_chains,
       [(d)-[:AFFECTS]->(:Component)<-[:IN_COMPONENT]-(w:Entity)
         WHERE w.kind IN ['ticket', 'incident'] | w.id] AS related_work,
       [(d)<-[:MENTIONS]-(:Source)-[:MENTIONS]->(w:Entity)
         WHERE w.kind IN ['ticket', 'incident'] | w.id] AS co_mentioned_work""",
)


def _connections(depth: int) -> Query:
    return Query(
        name=f"connections_depth_{depth}",
        cypher=f"""
MATCH (a:Entity {{id: $from_id}}), (b:Entity {{id: $to_id}})
MATCH p = (a)-[*1..{depth}]-(b)
WHERE ALL(r IN relationships(p) WHERE r.valid_to IS NULL)
RETURN [n IN nodes(p) | {{id: n.id, kind: n.kind, name: n.name}}] AS nodes,
       [r IN relationships(p) | {_REL_FIELDS}] AS rels,
       length(p) AS hops
ORDER BY hops
LIMIT $limit""",
    )


# Q9: bounded discovery of how two entities are connected (undirected).
# FalkorDB's shortestPath() is directed-only, so we enumerate bounded paths and
# order by length instead.
CONNECTIONS: dict[int, Query] = {d: _connections(d) for d in range(1, MAX_PATH_DEPTH + 1)}


def all_queries() -> list[Query]:
    """Every Cypher string this service can ever execute."""
    queries: list[Query] = [*UPSERT_ENTITY.values(), *UPSERT_RELATIONSHIP.values()]
    queries += [CLOSE_OTHER_OWNERS, SET_TICKET_STATUS, WITHDRAW_SOURCE]
    queries += [
        ENTITIES_EXIST,
        GET_ENTITY,
        RESOLVE_ENTITIES,
        TICKETS_IN_SCOPE,
        TICKET_STATUS,
        CURRENT_OWNERS,
        TICKET_INCIDENT_PATHS,
        INCIDENTS,
        SOURCES_FOR,
        EXPERTS_FOR_COMPONENTS,
        DECISIONS_AFFECTING,
        CURRENT_DECISIONS,
        DECISION_PROVENANCE,
        *CONNECTIONS.values(),
    ]
    return queries
