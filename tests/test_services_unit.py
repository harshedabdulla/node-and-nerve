"""Application services against the in-memory store: which queries run, with what params."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from company_brain.ingest import EntityNotFound, Ingestor
from company_brain.investigate import Investigator
from company_brain.models import (
    EntityInput,
    InvestigationRequest,
    OwnershipChanged,
    RelationshipInput,
)
from company_brain.schema import Rel
from tests.conftest import FakeStore

TS = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)


def test_upsert_entity_uses_kind_specific_template_and_parameters() -> None:
    store = FakeStore({"upsert_entity_ticket": [{"id": "ticket:a"}]})
    result = Ingestor(store, "northstar").upsert_entity(
        EntityInput(id="ticket:a", name="A", properties={"status": "open"}, observed_at=TS)
    )
    assert result.applied
    company, name, params = store.calls[-1]
    assert (company, name) == ("northstar", "upsert_entity_ticket")
    assert params == {
        "id": "ticket:a",
        "kind": "ticket",
        "ts": "2026-10-16T09:30:00Z",
        "props": {"name": "A", "status": "open"},
    }


def test_stale_entity_write_reports_not_applied() -> None:
    store = FakeStore({"upsert_entity_ticket": []})  # WHERE updated_at <= $ts filtered it out
    result = Ingestor(store, "northstar").upsert_entity(EntityInput(id="ticket:a", name="A"))
    assert not result.applied


def test_relationship_requires_existing_endpoints() -> None:
    store = FakeStore({"entities_exist": [{"id": "ticket:a", "kind": "ticket"}]})
    with pytest.raises(EntityNotFound) as err:
        Ingestor(store, "northstar").upsert_relationship(
            RelationshipInput(type=Rel.BLOCKED_BY, from_id="ticket:a", to_id="ticket:b")
        )
    assert err.value.missing == ["ticket:b"]
    assert [c[1] for c in store.calls] == ["entities_exist"]


def test_ownership_change_closes_previous_owner_then_links_new_one() -> None:
    store = FakeStore(
        {
            "entities_exist": [
                {"id": "ticket:a", "kind": "ticket"},
                {"id": "person:new", "kind": "person"},
            ],
            "close_other_owners": [{"previous_owner_id": "person:old"}],
        }
    )
    result = Ingestor(store, "northstar").apply_event(
        OwnershipChanged(
            type="ownership_changed",
            event_id="e1",
            entity_id="ticket:a",
            new_owner_id="person:new",
            occurred_at=TS,
        )
    )
    names = [c[1] for c in store.calls]
    assert names.index("close_other_owners") < names.index("upsert_relationship_owns")
    close_params = store.calls[names.index("close_other_owners")][2]
    assert close_params["owner_kind"] == "person"  # a team owner is left untouched
    assert result.changes == ["person:old no longer owns ticket:a", "person:new owns ticket:a"]


def test_investigation_without_matching_entities_reports_missing_information() -> None:
    store = FakeStore({"resolve_entities": []})
    store.companies.add("northstar")
    response = Investigator(store, "northstar").investigate(
        InvestigationRequest(question="Why is Project Zephyr late?", as_of=date(2026, 10, 16))
    )
    assert response.findings == []
    assert response.evidence_paths == []
    assert "No project, component or ticket" in response.missing_information[0]
    assert response.queries_executed == ["resolve_entities"]


def test_expertise_is_scored_only_from_recorded_relationships() -> None:
    store = FakeStore(
        {
            "experts_for_components": [
                {
                    "person_id": "person:a",
                    "name": "A",
                    "component_id": "component:x",
                    "basis": "resolved_incident",
                    "evidence_id": "incident:1",
                },
                {
                    "person_id": "person:b",
                    "name": "B",
                    "component_id": "component:x",
                    "basis": "member_of_owning_team",
                    "evidence_id": "team:t",
                },
                {
                    "person_id": "person:a",
                    "name": "A",
                    "component_id": "component:x",
                    "basis": "resolved_incident",
                    "evidence_id": "incident:1",
                },
            ]
        }
    )
    from company_brain.investigate import _Run

    experts = Investigator(store, "c")._experts(_Run(store, "c"), ["component:x"])
    assert [(e.person_id, e.score) for e in experts] == [("person:a", 3), ("person:b", 1)]
