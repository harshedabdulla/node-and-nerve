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

_NODE_FIELDS = (
    "{id: n.id, kind: n.kind, name: n.name, status: n.status, "
    "ref: n.source_record_id, required_by: n.required_by}"
)

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


# ---------------------------------------------------------------- Cashflow Radar
#
# Money is integer paise; dates are ISO strings. Only cleared payments reduce what
# a customer owes. A commitment depends on a collection only through an explicit
# DEPENDS_ON edge: the queries never derive a dependency from amounts or dates.

# Invoice statuses that take an invoice out of receivables regardless of payments.
CLOSED_INVOICE_STATUSES = ["cancelled", "written_off"]

# C1: every invoice with money still outstanding, computed by the graph from the
# payments allocated to it.
CF_RECEIVABLES = Query(
    name="cf_receivables",
    cypher="""
MATCH (i:Invoice)
WHERE NOT coalesce(i.status, 'open') IN $closed
OPTIONAL MATCH (p:Payment)-[a:ALLOCATED_TO]->(i)
WITH i,
     toInteger(sum(CASE WHEN p.status = 'cleared' THEN a.amount_paise ELSE 0 END)) AS cleared,
     collect(CASE WHEN p IS NULL THEN NULL ELSE {
         id: p.id, ref: p.source_record_id, status: p.status, received_on: p.received_on,
         amount_paise: a.amount_paise, source_id: a.source_id} END) AS allocations
WITH i, cleared, allocations, i.amount_paise - cleared AS outstanding
WHERE outstanding IS NULL OR outstanding > 0
RETURN properties(i) AS invoice,
       [(i)-[b:BILLED_TO]->(c:Customer) | {id: c.id, name: c.name, source_id: b.source_id}]
         AS customers,
       cleared AS cleared_paise, outstanding AS outstanding_paise, allocations,
       (i.due_date IS NOT NULL AND i.due_date < $as_of) AS overdue,
       [(i)-[:FOR_ORDER]->(o:SalesOrder) | {id: o.id, name: o.name, status: o.status}] AS orders
ORDER BY overdue DESC, i.due_date, i.id
LIMIT $limit""",
)

# C2: open commitments due on or before $until (including past-due ones), with
# superseded versions excluded by the graph, as for decisions. A cancelled
# replacement does not supersede anything.
CF_COMMITMENTS_DUE = Query(
    name="cf_commitments_due",
    cypher="""
MATCH (k:Commitment)
WHERE coalesce(k.status, 'open') = 'open' AND (k.due_date IS NULL OR k.due_date <= $until)
OPTIONAL MATCH (newer:Commitment)-[:SUPERSEDES]->(k)
WHERE coalesce(newer.status, 'open') <> 'cancelled'
WITH k, count(newer) AS superseders
WHERE superseders = 0
RETURN properties(k) AS commitment,
       [(k)-[ow:OWED_TO]->(x:Entity) | {id: x.id, kind: x.kind, name: x.name,
                                       source_id: ow.source_id}] AS owed_to,
       [(k)-[:FOR_ORDER]->(o:Entity) | {id: o.id, kind: o.kind, name: o.name,
                                       status: o.status}] AS orders,
       [(k)-[d:DEPENDS_ON]->(inv:Invoice) | {invoice_id: inv.id, ref: inv.source_record_id,
                                             source_id: d.source_id}] AS depends_on,
       [(k)-[sup:SUPERSEDES]->(old:Commitment) | {id: old.id, ref: old.source_record_id,
                                                 due_date: old.due_date,
                                                 amount_paise: old.amount_paise,
                                                 source_id: sup.source_id}] AS supersedes
ORDER BY k.due_date, k.id
LIMIT $limit""",
)

# C3: the core traversal. From each outstanding invoice, follow recorded
# DEPENDS_ON edges back to commitments, then out to who is owed and to the
# purchase order -> waiting sales order -> customer chain. Returns the paths.
CF_DEPENDENCY_PATHS = Query(
    name="cf_dependency_paths",
    cypher=f"""
UNWIND $invoice_ids AS iid
MATCH dep = (i:Invoice {{id: iid}})<-[d:DEPENDS_ON]-(k:Commitment)
OPTIONAL MATCH (newer:Commitment)-[:SUPERSEDES]->(k)
WHERE coalesce(newer.status, 'open') <> 'cancelled'
WITH iid, i, dep, d, k, collect(newer.id) AS superseded_by
OPTIONAL MATCH billed = (i)-[:BILLED_TO]->(:Customer)
OPTIONAL MATCH owed = (k)-[:OWED_TO]->(:Entity)
OPTIONAL MATCH chain = (k)-[:FOR_ORDER]->(:PurchaseOrder)<-[:SOURCED_FROM]-(:SalesOrder)
                       -[:PLACED_BY]->(:Customer)
RETURN iid AS invoice_id, k.id AS commitment_id, properties(k) AS commitment,
       superseded_by, d.source_id AS dependency_source_id, d.note AS note,
       CASE WHEN billed IS NULL THEN [] ELSE [r IN relationships(billed) | {_REL_FIELDS}] END
         + [r IN relationships(dep) | {_REL_FIELDS}] AS dependency_rels,
       CASE WHEN owed IS NULL THEN [] ELSE [r IN relationships(owed) | {_REL_FIELDS}] END
         AS owed_rels,
       CASE WHEN owed IS NULL THEN [] ELSE [n IN nodes(owed) | {_NODE_FIELDS}] END AS owed_nodes,
       CASE WHEN chain IS NULL THEN [] ELSE [r IN relationships(chain) | {_REL_FIELDS}] END
         AS order_rels,
       CASE WHEN chain IS NULL THEN [] ELSE [n IN nodes(chain) | {_NODE_FIELDS}] END
         AS order_nodes
ORDER BY invoice_id, commitment_id""",
)

# M9: display labels for a set of entities (trace pages).
MF_LABELS = Query(
    name="mf_labels",
    cypher="""
UNWIND $ids AS eid
MATCH (n:Entity {id: eid})
RETURN n.id AS id, n.kind AS kind, coalesce(n.source_record_id, n.id) AS ref, n.name AS name,
       n.status AS status, n.amount_paise AS amount_paise""",
)


# Note (FalkorDB v4.20.7): a variable name reused across pattern comprehensions in one
# RETURN can make later comprehensions come back empty, so each uses its own names.

# Sources cited on edges (for example the message that documents a dependency).
CF_SOURCES_BY_ID = Query(
    name="cf_sources_by_id",
    cypher=f"""
UNWIND $ids AS sid
MATCH (s:Source {{id: sid}})
RETURN {_SOURCE_FIELDS} AS source
ORDER BY s.id""",
)


# ---------------------------------------------------------------- Contract costing
#
# A cost reaches a contract only through CHARGED_TO edges into the contract or one of
# its jobs, and only the amount on that edge counts (a shared bill is split, never
# copied). NOTED_FOR marks a cost the books associate with a job or contract without
# an amount: a pending decision, never a cost. Payments settle bills through
# ALLOCATED_TO; they are never costs. Voided book entries are returned so they can be
# reported, and are excluded from every total.

_COST_RECORD_LINKS = """
       [(rec)-[o:CHARGED_TO]->(xc:Entity) | {id: xc.id, amount_paise: o.amount_paise}]
         AS all_charges,
       [(rec)-[:NOTED_FOR]->(xn:Entity) | xn.id] AS noted_for,
       [(rec)-[:SUPPLIED_BY]->(s:Supplier) | {id: s.id, name: s.name}] AS suppliers,
       [(rec)-[:EARNED_BY]->(w:Worker) | {id: w.id, name: w.name, role: w.role}] AS workers,
       [(pay:Payment)-[a:ALLOCATED_TO]->(rec) | {id: pay.id, ref: pay.source_record_id,
         status: pay.status, paid_on: pay.paid_on, amount_paise: a.amount_paise,
         source_id: a.source_id}] AS payments,
       [(rec)-[:SUPPORTED_BY]->(src:Source) WHERE coalesce(src.status, 'active') = 'active'
         | src.id] AS source_ids"""

# M1: the contract, its customer, jobs and customer receipts.
MF_CONTRACT = Query(
    name="mf_contract",
    cypher="""
MATCH (c:Contract {id: $id})
RETURN properties(c) AS contract,
       [(c)-[pb:PLACED_BY]->(cu:Customer) | {id: cu.id, name: cu.name, source_id: pb.source_id}]
         AS customers,
       [(j:Job)-[po:PART_OF]->(c) | {id: j.id, ref: j.source_record_id, name: j.name,
         status: j.status, started_on: j.started_on, completed_on: j.completed_on,
         source_id: po.source_id}] AS jobs,
       [(p:Payment)-[a:ALLOCATED_TO]->(c) | {id: p.id, ref: p.source_record_id,
         status: p.status, received_on: p.received_on, method: p.method,
         reference: p.reference, amount_paise: a.amount_paise, source_id: a.source_id}]
         AS receipts,
       [(c)-[:SUPPORTED_BY]->(s:Source) WHERE coalesce(s.status, 'active') = 'active' | s.id]
         AS source_ids""",
)

# M2: every cost charged to the contract or one of its jobs, one row per charge, with
# the traversed path and the record's supplier or worker, payments and sources.
MF_CONTRACT_CHARGES = Query(
    name="mf_contract_charges",
    cypher=f"""
MATCH path = (c:Contract {{id: $id}})<-[:PART_OF*0..1]-(t:Entity)<-[ch:CHARGED_TO]-(rec:Entity)
WHERE rec.kind IN $cost_kinds
RETURN rec.kind AS kind, properties(rec) AS record, t.id AS charged_to,
       t.source_record_id AS charged_to_ref, ch.amount_paise AS charged_paise,
       ch.source_id AS charge_source_id,
       [r IN relationships(path) | {_REL_FIELDS}] AS rels,{_COST_RECORD_LINKS}
ORDER BY rec.kind, rec.source_record_id, t.id
LIMIT $limit""",
)

# M3: category totals computed by the graph (cross-checked against M2's rows).
MF_CONTRACT_COST_TOTALS = Query(
    name="mf_contract_cost_totals",
    cypher="""
MATCH (c:Contract {id: $id})<-[:PART_OF*0..1]-(t:Entity)<-[ch:CHARGED_TO]-(rec:Entity)
WHERE rec.kind IN $cost_kinds AND coalesce(rec.status, 'recorded') <> 'void'
RETURN rec.kind AS kind, toInteger(sum(ch.amount_paise)) AS charged_paise,
       count(ch) AS charges,
       toInteger(sum(CASE WHEN ch.amount_paise IS NULL THEN 1 ELSE 0 END))
         AS charges_without_amount
ORDER BY kind""",
)

# M4: costs the books associate with the contract or its jobs without an amount.
MF_CONTRACT_NOTED = Query(
    name="mf_contract_noted",
    cypher=f"""
MATCH (c:Contract {{id: $id}})<-[:PART_OF*0..1]-(t:Entity)<-[n:NOTED_FOR]-(rec:Entity)
WHERE rec.kind IN $cost_kinds AND coalesce(rec.status, 'recorded') <> 'void'
WITH rec, collect(DISTINCT t.id) AS noted_here, collect(DISTINCT n.source_id) AS note_sources
RETURN rec.kind AS kind, properties(rec) AS record, noted_here, note_sources,{_COST_RECORD_LINKS}
ORDER BY rec.source_record_id
LIMIT $limit""",
)

# M5: company-wide cost records not (fully) charged to any job or contract. A charge only
# counts if it reaches a contract (directly or via PART_OF); others are unlinked_targets.
MF_UNALLOCATED = Query(
    name="mf_unallocated",
    cypher=f"""
MATCH (rec:Entity)
WHERE rec.kind IN $cost_kinds AND coalesce(rec.status, 'recorded') <> 'void'
OPTIONAL MATCH (rec)-[ch:CHARGED_TO]->(t:Entity)
WITH rec, ch, t, t:Contract OR size([(t)-[:PART_OF]->(lc:Contract) | lc]) > 0 AS linked
WITH rec, toInteger(sum(CASE WHEN linked THEN ch.amount_paise ELSE 0 END)) AS charged,
     [x IN collect(CASE WHEN ch IS NOT NULL AND NOT linked THEN t.id END) WHERE x IS NOT NULL]
       AS unlinked_targets
WHERE rec.amount_paise IS NULL OR charged < rec.amount_paise
RETURN rec.kind AS kind, properties(rec) AS record, charged AS charged_paise,
       unlinked_targets,{_COST_RECORD_LINKS},
       [(rec)-[:NOTED_FOR]->(xt:Entity) | {{id: xt.id, kind: xt.kind, ref: xt.source_record_id}}]
         AS noted_targets
ORDER BY rec.source_record_id
LIMIT $limit""",
)

# M6: supplier payments not (fully) applied to any bill; $supplier_ids null = all.
MF_UNAPPLIED_PAYMENTS = Query(
    name="mf_unapplied_payments",
    cypher="""
MATCH (p:Payment)-[:PAID_TO]->(s:Supplier)
WHERE $supplier_ids IS NULL OR s.id IN $supplier_ids
OPTIONAL MATCH (p)-[a:ALLOCATED_TO]->(:Entity)
WITH p, s, toInteger(sum(a.amount_paise)) AS applied
WHERE applied < p.amount_paise
RETURN p.id AS id, properties(p) AS payment, s.id AS supplier_id, s.name AS supplier_name,
       applied AS applied_paise,
       [(p)-[:SUPPORTED_BY]->(src:Source) | src.id] AS source_ids
ORDER BY p.id""",
)

# M7: every contract with graph-computed recorded cost and cleared receipts.
MF_CONTRACTS_OVERVIEW = Query(
    name="mf_contracts_overview",
    cypher="""
MATCH (c:Contract)
OPTIONAL MATCH (c)<-[:PART_OF*0..1]-(:Entity)<-[ch:CHARGED_TO]-(rec:Entity)
WHERE coalesce(rec.status, 'recorded') <> 'void'
WITH c, toInteger(sum(ch.amount_paise)) AS cost, count(ch) AS charge_lines,
     toInteger(sum(CASE WHEN ch IS NOT NULL AND ch.amount_paise IS NULL THEN 1 ELSE 0 END))
       AS charges_without_amount
OPTIONAL MATCH (p:Payment)-[a:ALLOCATED_TO]->(c)
WITH c, cost, charge_lines, charges_without_amount,
     toInteger(sum(CASE WHEN p.status = 'cleared' THEN a.amount_paise ELSE 0 END)) AS received
RETURN properties(c) AS contract,
       [(c)-[:PLACED_BY]->(cu:Customer) | cu.name] AS customers,
       [(j:Job)-[:PART_OF]->(c) | j.id] AS job_ids,
       cost AS recorded_cost_paise, charge_lines, charges_without_amount,
       received AS received_paise
ORDER BY c.contract_date, c.id
LIMIT $limit""",
)

# M8: cost records, payments and allocations added or changed after a timestamp.
MF_CHANGED_SINCE = Query(
    name="mf_changed_since",
    cypher="""
MATCH (rec:Entity)
WHERE rec.kind IN $kinds AND rec.updated_at > $since
RETURN 'record' AS change, rec.kind AS kind, properties(rec) AS record,
       NULL AS target_id, NULL AS amount_paise
UNION ALL
MATCH (rec:Entity)-[o:CHARGED_TO]->(x:Entity)
WHERE o.created_at > $since
RETURN 'allocation' AS change, rec.kind AS kind, properties(rec) AS record,
       x.id AS target_id, o.amount_paise AS amount_paise""",
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
        CF_RECEIVABLES,
        CF_COMMITMENTS_DUE,
        CF_DEPENDENCY_PATHS,
        CF_SOURCES_BY_ID,
        MF_CONTRACT,
        MF_CONTRACT_CHARGES,
        MF_CONTRACT_COST_TOTALS,
        MF_CONTRACT_NOTED,
        MF_UNALLOCATED,
        MF_UNAPPLIED_PAYMENTS,
        MF_CONTRACTS_OVERVIEW,
        MF_CHANGED_SINCE,
        MF_LABELS,
    ]
    return queries
