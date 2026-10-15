"""Integration tests against a real FalkorDB. Every required query is exercised here."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from company_brain import queries as q
from company_brain.evaluation import SCENARIOS, score_baseline, score_graph
from company_brain.graph import FalkorDBStore, GraphConflict
from company_brain.ingest import EntityNotFound, Ingestor
from company_brain.investigate import Investigator
from company_brain.lookup import Lookup
from company_brain.models import (
    DecisionSuperseded,
    EntityInput,
    IncidentRecorded,
    InvestigationRequest,
    OwnershipChanged,
    RelationshipInput,
    SourceInput,
    SourceWithdrawn,
    TicketStatusChanged,
)
from company_brain.schema import Rel
from company_brain.seed import load_dataset, seed

AS_OF = date(2026, 10, 16)
LATER = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _counts(raw_graph: Any, company: str) -> tuple[int, int]:
    graph = raw_graph(company)
    nodes = graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = graph.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return nodes, edges


def _run(store: FalkorDBStore, company: str, query: q.Query, **params: Any) -> list[dict[str, Any]]:
    return store.run(company, query, params)


# ---------------------------------------------------------------- seed and schema


def test_seed_is_idempotent(real_store: FalkorDBStore, company: str, raw_graph: Any) -> None:
    dataset = load_dataset()
    seed(real_store, dataset, company)
    first = _counts(raw_graph, company)
    props_first = (
        raw_graph(company).ro_query("MATCH (n) RETURN n.id, properties(n) ORDER BY n.id").result_set
    )
    seed(real_store, dataset, company)
    seed(real_store, dataset, company)
    assert _counts(raw_graph, company) == first
    expected_nodes = len(dataset["entities"]) + len(dataset["sources"])
    assert first[0] == expected_nodes
    assert (
        raw_graph(company).ro_query("MATCH (n) RETURN n.id, properties(n) ORDER BY n.id").result_set
        == props_first
    )


def test_unique_constraint_is_enforced(seeded: str, raw_graph: Any) -> None:
    constraints = raw_graph(seeded).list_constraints()
    assert {"label": "Entity", "properties": ["id"], "type": "UNIQUE"}.items() <= constraints[
        0
    ].items()
    with pytest.raises(Exception, match="constraint"):
        raw_graph(seeded).query("CREATE (:Entity {id: 'ticket:atlas-142'})")


def test_conflict_is_translated(real_store: FalkorDBStore, seeded: str) -> None:
    clash = q.Query(name="test_clash", read_only=False, cypher="CREATE (:Entity {id: $id})")
    with pytest.raises(GraphConflict):
        real_store.run(seeded, clash, {"id": "ticket:atlas-142"})


# ---------------------------------------------------------------- the nine query capabilities


def test_q1_project_blocked_or_overdue_tickets(real_store: FalkorDBStore, seeded: str) -> None:
    scope = [
        r["id"] for r in _run(real_store, seeded, q.TICKETS_IN_SCOPE, scope_id="project:atlas")
    ]
    assert scope == ["ticket:atlas-130", "ticket:atlas-142", "ticket:atlas-147", "ticket:atlas-155"]
    rows = _run(
        real_store,
        seeded,
        q.TICKET_STATUS,
        ticket_ids=scope,
        as_of="2026-10-16",
        closed=q.CLOSED_STATUSES,
    )
    by_id = {r["ticket"]["id"]: r for r in rows}
    assert (
        by_id["ticket:atlas-142"]["overdue"]
        and by_id["ticket:atlas-142"]["blockers"][0]["id"] == "ticket:int-88"
    )
    assert by_id["ticket:atlas-147"]["overdue"] and not by_id["ticket:atlas-147"]["blockers"]
    assert not by_id["ticket:atlas-130"]["overdue"]  # done
    assert not by_id["ticket:atlas-155"]["overdue"]  # due later


def test_q2_trace_tickets_to_incidents(real_store: FalkorDBStore, seeded: str) -> None:
    rows = _run(
        real_store,
        seeded,
        q.TICKET_INCIDENT_PATHS,
        ticket_ids=["ticket:atlas-142"],
        closed=q.CLOSED_STATUSES,
    )
    direct = [r for r in rows if r["relation"] == "affects_blocker_chain"]
    assert [r["incident_id"] for r in direct] == ["incident:inc-009"]
    assert [(e["from"], e["type"], e["to"]) for e in direct[0]["rels"]] == [
        ("ticket:atlas-142", "BLOCKED_BY", "ticket:int-88"),
        ("incident:inc-009", "AFFECTS", "ticket:int-88"),
    ]
    assert direct[0]["rels"][0]["source_id"] == "source:jira-atlas-142"
    similar = {r["incident_id"] for r in rows if r["relation"] == "same_component"}
    assert "incident:inc-007" in similar
    assert "incident:inc-011" not in {r["incident_id"] for r in rows}


def test_q3_sources_distinguish_support_mention_contradiction(
    real_store: FalkorDBStore, seeded: str
) -> None:
    rows = _run(real_store, seeded, q.SOURCES_FOR, ids=["incident:inc-009"])
    by_relation: dict[str, set[str]] = {}
    for r in rows:
        by_relation.setdefault(r["relation"], set()).add(r["source"]["id"])
    assert by_relation == {
        "supports": {"source:postmortem-009", "source:chat-inc-009"},
        "mentions": {"source:jira-int-88"},
        "contradicts": {"source:vendor-status-0922"},
    }
    pm = next(r["source"] for r in rows if r["source"]["id"] == "source:postmortem-009")
    assert pm["uri"] == "https://docs.northstar.example/postmortems/inc-009"
    assert pm["authors"] == ["person:sam-okafor"]


def test_q4_current_ownership(real_store: FalkorDBStore, seeded: str) -> None:
    rows = _run(real_store, seeded, q.CURRENT_OWNERS, ids=["ticket:atlas-142", "project:atlas"])
    assert {(r["entity_id"], r["owner_id"]) for r in rows} == {
        ("ticket:atlas-142", "person:dev-patel"),
        ("project:atlas", "person:maya-chen"),
        ("project:atlas", "team:platform"),
    }
    dev = next(r for r in rows if r["owner_id"] == "person:dev-patel")
    assert dev["owner_teams"] == ["team:platform"]


def test_q5_expertise_from_recorded_relationships(real_store: FalkorDBStore, seeded: str) -> None:
    rows = _run(
        real_store, seeded, q.EXPERTS_FOR_COMPONENTS, component_ids=["component:billing-gateway"]
    )
    basis = {(r["person_id"], r["basis"], r["evidence_id"]) for r in rows}
    assert ("person:lena-ortiz", "resolved_incident", "incident:inc-007") in basis
    assert ("person:sam-okafor", "owns_component", "component:billing-gateway") in basis
    assert all(r["component_id"] == "component:billing-gateway" for r in rows)
    assert "person:arjun-mehta" not in {r["person_id"] for r in rows}


def test_q6_q8_decisions_and_current_guidance(real_store: FalkorDBStore, seeded: str) -> None:
    targets = ["component:billing-gateway", "project:atlas"]
    rows = _run(real_store, seeded, q.DECISIONS_AFFECTING, target_ids=targets)
    by_id = {r["decision"]["id"]: r for r in rows}
    assert set(by_id) == {"decision:dec-002", "decision:dec-004", "decision:dec-005"}
    assert by_id["decision:dec-002"]["superseded_by"] == ["decision:dec-004"]
    assert by_id["decision:dec-004"]["supersedes"] == ["decision:dec-002"]
    current = [r["id"] for r in _run(real_store, seeded, q.CURRENT_DECISIONS, target_ids=targets)]
    assert current == ["decision:dec-004", "decision:dec-005"]


def test_q7_decision_provenance(real_store: FalkorDBStore, seeded: str) -> None:
    prov = Lookup(real_store, seeded).decision_sources("decision:dec-004")
    assert prov.current
    assert [s.id for s in prov.supporting_sources] == ["source:arch-review-2026-07"]
    # the postmortem mentions DEC-004 but is not evidence for it
    assert [s.id for s in prov.mentioning_sources] == ["source:postmortem-009"]
    assert prov.decided_by == [{"id": "person:priya-nair", "name": "Priya Nair"}]
    assert prov.supersedes_chains == [["decision:dec-004", "decision:dec-002"]]
    assert {"ticket:int-88", "incident:inc-009", "incident:inc-007"} <= set(prov.related_work)
    old = Lookup(real_store, seeded).decision_sources("decision:dec-002")
    assert not old.current and old.superseded_by == ["decision:dec-004"]


def test_q9_bounded_connection_discovery(real_store: FalkorDBStore, seeded: str) -> None:
    lookup = Lookup(real_store, seeded)
    paths = lookup.connections("ticket:atlas-142", "incident:inc-009", max_depth=2)
    assert paths and paths[0].hops == 2
    assert [(e.from_id, e.type, e.to_id) for e in paths[0].edges] == [
        ("ticket:atlas-142", "BLOCKED_BY", "ticket:int-88"),
        ("incident:inc-009", "AFFECTS", "ticket:int-88"),
    ]
    assert lookup.connections("ticket:atlas-142", "incident:inc-009", max_depth=1) == []
    assert len(lookup.connections("project:atlas", "person:priya-nair", max_depth=4, limit=3)) <= 3
    with pytest.raises(ValueError):
        lookup.connections("ticket:atlas-142", "incident:inc-009", max_depth=9)


# ---------------------------------------------------------------- end-to-end investigation


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_investigation_scenarios_meet_expectations(
    real_store: FalkorDBStore, seeded: str, scenario: Any
) -> None:
    response = Investigator(real_store, seeded).investigate(
        InvestigationRequest(question=scenario.question, as_of=scenario.as_of)
    )
    score = score_graph(scenario, response)
    assert score.missing_entities == []
    assert score.path_found
    assert score.missing_sources == []
    assert score.superseded_excluded
    assert score.missing_evidence_flagged
    assert score.leaked_entities == []
    baseline = score_baseline(scenario, load_dataset())
    assert baseline.entity_recall < score.entity_recall
    assert not baseline.path_found


def test_atlas_investigation_details(real_store: FalkorDBStore, seeded: str) -> None:
    r = Investigator(real_store, seeded).investigate(
        InvestigationRequest(question=SCENARIOS[0].question, as_of=AS_OF)
    )
    assert [e.id for e in r.matched_entities] == ["project:atlas"]
    chain = next(p for p in r.evidence_paths if p.description.startswith("evidence chain"))
    assert [(e.from_id, e.type, e.to_id) for e in chain.edges][-3:] == [
        ("incident:inc-009", "SUPPORTED_BY", "source:postmortem-009"),
        ("source:postmortem-009", "MENTIONS", "decision:dec-004"),
        ("decision:dec-004", "DECIDED_BY", "person:priya-nair"),
    ]
    assert any("source:vendor-status-0922 contradicts incident:inc-009" in w for w in r.warnings)
    assert "No current owner is recorded for ticket:atlas-147." in r.missing_information
    assert all(f.statement for f in r.findings)
    # findings cite paths that exist in the response
    path_ids = {p.id for p in r.evidence_paths}
    assert all(set(f.path_ids) <= path_ids for f in r.findings)


def test_explicit_focus_on_missing_entity_warns(real_store: FalkorDBStore, seeded: str) -> None:
    r = Investigator(real_store, seeded).investigate(
        InvestigationRequest(question="status?", focus_ids=["project:zephyr"], as_of=AS_OF)
    )
    assert r.findings == []
    assert "project:zephyr does not exist" in r.warnings[0]


# ---------------------------------------------------------------- updates keep knowledge current


def _atlas(real_store: FalkorDBStore, company: str) -> Any:
    return Investigator(real_store, company).investigate(
        InvestigationRequest(question="Atlas status", focus_ids=["project:atlas"], as_of=AS_OF)
    )


def test_ownership_change_is_current_and_idempotent(
    real_store: FalkorDBStore, seeded: str, raw_graph: Any
) -> None:
    ingest = Ingestor(real_store, seeded)
    event = OwnershipChanged(
        type="ownership_changed",
        event_id="e1",
        entity_id="ticket:atlas-142",
        new_owner_id="person:lena-ortiz",
        occurred_at=LATER,
    )
    ingest.apply_event(event)
    counts = _counts(raw_graph, seeded)
    again = ingest.apply_event(event)
    assert _counts(raw_graph, seeded) == counts
    assert again.changes == ["person:lena-ortiz owns ticket:atlas-142"]
    owners = {
        o.owner_id
        for o in _atlas(real_store, seeded).current_owners
        if o.entity_id == "ticket:atlas-142"
    }
    assert owners == {"person:lena-ortiz"}
    # history is kept, not deleted
    closed = (
        raw_graph(seeded)
        .ro_query(
            "MATCH (:Person {id: 'person:dev-patel'})-[o:OWNS]->(:Ticket {id: 'ticket:atlas-142'}) "
            "RETURN o.valid_to, o.closed_by_event"
        )
        .result_set
    )
    assert closed == [["2026-10-16T12:00:00Z", "e1"]]


def test_ticket_status_change_removes_blocker_from_answer(
    real_store: FalkorDBStore, seeded: str
) -> None:
    Ingestor(real_store, seeded).apply_event(
        TicketStatusChanged(
            type="ticket_status_changed",
            event_id="e2",
            ticket_id="ticket:int-88",
            status="done",
            occurred_at=LATER,
        )
    )
    r = _atlas(real_store, seeded)
    assert not [f for f in r.findings if f.category == "blocker_dependency"]
    assert not [f for f in r.findings if f.category == "incident"]
    assert any(
        "ticket:atlas-142 is marked blocked but no open blocker" in m for m in r.missing_information
    )


def test_stale_writes_do_not_overwrite_newer_state(real_store: FalkorDBStore, seeded: str) -> None:
    ingest = Ingestor(real_store, seeded)
    ingest.apply_event(
        TicketStatusChanged(
            type="ticket_status_changed",
            event_id="e3",
            ticket_id="ticket:int-88",
            status="done",
            occurred_at=LATER,
        )
    )
    stale = ingest.upsert_entity(
        EntityInput(
            id="ticket:int-88",
            name="old",
            properties={"status": "blocked"},
            observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    )
    assert not stale.applied
    entity = Lookup(real_store, seeded).entity("ticket:int-88").entity
    assert (
        entity["status"] == "done" and entity["name"] == "Enable v2 webhook signing in production"
    )


def test_withdrawn_source_stops_counting_as_evidence(
    real_store: FalkorDBStore, seeded: str
) -> None:
    ingest = Ingestor(real_store, seeded)
    for sid in ("source:postmortem-009", "source:chat-inc-009"):
        ingest.apply_event(
            SourceWithdrawn(
                type="source_withdrawn",
                event_id=f"w-{sid}",
                source_id=sid,
                reason="retracted",
                occurred_at=LATER,
            )
        )
    r = _atlas(real_store, seeded)
    incident = next(
        f for f in r.findings if "incident:inc-009" in f.entity_ids and f.category == "incident"
    )
    assert not incident.supported and incident.source_ids == []
    assert "incident:inc-009 has no active supporting source record." in r.missing_information
    assert not [p for p in r.evidence_paths if p.description.startswith("evidence chain")]


def test_new_decision_supersedes_and_changes_current_guidance(
    real_store: FalkorDBStore, seeded: str
) -> None:
    ingest = Ingestor(real_store, seeded)
    ingest.upsert_entity(
        EntityInput(
            id="decision:dec-007",
            name="Allow temporary v1 fallback for Atlas",
            properties={"status": "accepted", "decided_at": "2026-10-16"},
        )
    )
    ingest.upsert_relationship(
        RelationshipInput(type=Rel.AFFECTS, from_id="decision:dec-007", to_id="project:atlas")
    )
    ingest.register_source(
        SourceInput(
            id="source:exception-memo",
            name="Exception memo",
            source_type="document",
            supports=["decision:dec-007"],
        )
    )
    ingest.apply_event(
        DecisionSuperseded(
            type="decision_superseded",
            event_id="e4",
            new_decision_id="decision:dec-007",
            old_decision_id="decision:dec-004",
        )
    )
    current = {d.id for d in _atlas(real_store, seeded).decisions if d.current}
    assert current == {"decision:dec-005", "decision:dec-007"}


def test_incident_recorded_event_links_into_investigation(
    real_store: FalkorDBStore, seeded: str
) -> None:
    ingest = Ingestor(real_store, seeded)
    ingest.register_source(
        SourceInput(
            id="source:pm-012",
            name="PM-012",
            source_type="postmortem",
            uri="https://docs.northstar.example/pm/12",
        )
    )
    event = IncidentRecorded(
        type="incident_recorded",
        event_id="e5",
        incident=EntityInput(
            id="incident:inc-012",
            name="Onboarding API 5xx spike",
            properties={"status": "open", "severity": "sev2"},
        ),
        affects=["ticket:atlas-147"],
        component_ids=["component:onboarding-api"],
        source_ids=["source:pm-012"],
        occurred_at=LATER,
    )
    ingest.apply_event(event)
    ingest.apply_event(event)
    r = _atlas(real_store, seeded)
    inc = [
        f
        for f in r.findings
        if f.category == "incident" and f.entity_ids[:2] == ["incident:inc-012", "ticket:atlas-147"]
    ]
    assert len(inc) == 1 and inc[0].supported and inc[0].source_ids == ["source:pm-012"]
    # the overdue ticket is now explained by a linked incident
    assert not any("ticket:atlas-147 is overdue but" in m for m in r.missing_information)


def test_relationship_to_missing_entity_is_rejected(real_store: FalkorDBStore, seeded: str) -> None:
    with pytest.raises(EntityNotFound):
        Ingestor(real_store, seeded).upsert_relationship(
            RelationshipInput(type=Rel.BLOCKED_BY, from_id="ticket:atlas-142", to_id="ticket:ghost")
        )


# ---------------------------------------------------------------- tenant isolation


def test_tenants_are_isolated(real_store: FalkorDBStore, seeded: str, company: str) -> None:
    other = f"{seeded}-b"
    try:
        Ingestor(real_store, other).upsert_entity(
            EntityInput(id="project:atlas", name="Other Atlas")
        )
        r = Investigator(real_store, other).investigate(
            InvestigationRequest(question="Why is Atlas late?", as_of=AS_OF)
        )
        ids = {i for f in r.findings for i in f.entity_ids} | {s.id for s in r.sources}
        assert not ids & {"ticket:atlas-142", "incident:inc-009", "source:postmortem-009"}
        with pytest.raises(EntityNotFound):
            Lookup(real_store, other).entity("ticket:atlas-142")
        assert (
            Lookup(real_store, seeded)
            .entity("project:atlas")
            .entity["name"]
            .startswith("Project Atlas")
        )
    finally:
        real_store.drop_company(other)
