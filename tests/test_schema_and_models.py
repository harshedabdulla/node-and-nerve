from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from company_brain.models import (
    EntityInput,
    Event,
    InvestigationRequest,
    RelationshipInput,
    SourceInput,
)
from company_brain.schema import GRAPH_PREFIX, Kind, Rel, check_relationship, graph_name, kind_of


@pytest.mark.parametrize(
    ("entity_id", "kind"),
    [
        ("ticket:atlas-142", Kind.TICKET),
        ("person:sana-k", Kind.PERSON),
        ("source:pm.9_a", Kind.SOURCE),
    ],
)
def test_kind_of_valid_ids(entity_id: str, kind: Kind) -> None:
    assert kind_of(entity_id) is kind


@pytest.mark.parametrize(
    "entity_id",
    ["atlas-142", "Ticket:atlas", "ticket:", "ticket:Atlas", "robot:r2", "ticket:a b", "ticket:a}"],
)
def test_kind_of_rejects_bad_ids(entity_id: str) -> None:
    with pytest.raises(ValueError):
        kind_of(entity_id)


def test_graph_name_is_derived_with_fixed_prefix() -> None:
    assert graph_name("northstar") == GRAPH_PREFIX + "northstar"
    assert graph_name("acme-demo") == GRAPH_PREFIX + "acme_demo"


@pytest.mark.parametrize(
    "company_id",
    ["", "Northstar", "a b", "../etc", "x;GRAPH.DELETE y", "a_b", "-lead", "trail-", "a" * 49, "*"],
)
def test_graph_name_rejects_unsafe_company_ids(company_id: str) -> None:
    with pytest.raises(ValueError):
        graph_name(company_id)


def test_relationship_rules() -> None:
    check_relationship(Rel.OWNS, Kind.PERSON, Kind.TICKET)
    with pytest.raises(ValueError, match="not allowed"):
        check_relationship(Rel.OWNS, Kind.TICKET, Kind.PERSON)
    with pytest.raises(ValueError):
        check_relationship(Rel.SUPERSEDES, Kind.DECISION, Kind.TICKET)


def test_entity_input_validates_properties_per_kind() -> None:
    ok = EntityInput(
        id="ticket:t-1", name="T", properties={"status": "blocked", "due_date": "2026-10-01"}
    )
    assert ok.kind is Kind.TICKET
    with pytest.raises(ValidationError, match="not allowed"):
        EntityInput(id="ticket:t-1", name="T", properties={"salary": 10})
    with pytest.raises(ValidationError, match="status"):
        EntityInput(id="ticket:t-1", name="T", properties={"status": "stuck"})
    with pytest.raises(ValidationError):
        EntityInput(id="ticket:t-1", name="T", properties={"due_date": "next week"})
    with pytest.raises(ValidationError):
        EntityInput(id="ticket:t-1", name="T", extra_field=1)  # type: ignore[call-arg]


def test_relationship_input_checks_endpoint_kinds() -> None:
    RelationshipInput(
        type=Rel.BLOCKED_BY, from_id="ticket:a", to_id="ticket:b", source_id="source:s"
    )
    with pytest.raises(ValidationError, match="not allowed"):
        RelationshipInput(type=Rel.RESOLVED, from_id="ticket:a", to_id="incident:b")
    with pytest.raises(ValidationError, match="source_id"):
        RelationshipInput(
            type=Rel.BLOCKED_BY, from_id="ticket:a", to_id="ticket:b", source_id="ticket:c"
        )
    with pytest.raises(ValidationError):
        RelationshipInput(type="DROP_ALL", from_id="ticket:a", to_id="ticket:b")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="may not set"):
        RelationshipInput(
            type=Rel.OWNS, from_id="person:a", to_id="ticket:b", properties={"valid_to": "x"}
        )


def test_source_input_distinguishes_support_from_mention() -> None:
    SourceInput(
        id="source:pm-1",
        name="PM",
        source_type="postmortem",
        supports=["incident:i-1"],
        mentions=["decision:d-1"],
    )
    with pytest.raises(ValidationError):
        # a person cannot be "supported by" a source
        SourceInput(id="source:pm-1", name="PM", source_type="postmortem", supports=["person:p"])
    with pytest.raises(ValidationError):
        SourceInput(id="ticket:x", name="PM", source_type="postmortem")
    with pytest.raises(ValidationError):
        SourceInput(id="source:pm-1", name="PM", source_type="tweet")


def test_events_are_a_validated_discriminated_union() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    event = adapter.validate_python(
        {
            "type": "ownership_changed",
            "event_id": "e1",
            "entity_id": "ticket:a",
            "new_owner_id": "person:b",
        }
    )
    assert event.type == "ownership_changed"
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {"type": "run_cypher", "event_id": "e1", "query": "MATCH (n) DELETE n"}
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "type": "ticket_status_changed",
                "event_id": "e1",
                "ticket_id": "ticket:a",
                "status": "lol",
            }
        )
    with pytest.raises(ValidationError, match="itself"):
        adapter.validate_python(
            {
                "type": "decision_superseded",
                "event_id": "e",
                "new_decision_id": "decision:a",
                "old_decision_id": "decision:a",
            }
        )


def test_investigation_focus_kinds_are_limited() -> None:
    InvestigationRequest(question="why late", focus_ids=["project:atlas"])
    with pytest.raises(ValidationError):
        InvestigationRequest(question="why late", focus_ids=["person:a"])
