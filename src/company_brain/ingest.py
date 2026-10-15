"""Deterministic, idempotent writes: entities, relationships, sources and change events.

Every write is a MERGE keyed on stable ids, so replaying the same input produces
the same graph. Entity writes carry an ``observed_at`` timestamp and are ignored
when the graph already holds a newer version of the record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from company_brain import queries as q
from company_brain.graph import GraphStore, Query
from company_brain.models import (
    DecisionSuperseded,
    EntityInput,
    Event,
    EventResult,
    IncidentRecorded,
    OwnershipChanged,
    RelationshipInput,
    SourceInput,
    SourceWithdrawn,
    TicketStatusChanged,
    WriteResult,
    iso,
    utc_now,
)
from company_brain.schema import Kind, Rel, kind_of


class EntityNotFound(LookupError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"entities not found: {', '.join(missing)}")
        self.missing = missing


def _ts(value: datetime | None) -> str:
    return iso(value or utc_now())


class Ingestor:
    def __init__(self, store: GraphStore, company_id: str) -> None:
        self.store = store
        self.company_id = company_id

    def _run(self, query: Query, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.run(self.company_id, query, params)

    def require(self, ids: list[str]) -> None:
        wanted = sorted(set(ids))
        if not wanted:
            return
        found = {row["id"] for row in self._run(q.ENTITIES_EXIST, {"ids": wanted})}
        missing = [i for i in wanted if i not in found]
        if missing:
            raise EntityNotFound(missing)

    # ------------------------------------------------------------ primitives

    def upsert_entity(self, entity: EntityInput) -> WriteResult:
        self.store.ensure_schema(self.company_id)
        props: dict[str, Any] = {"name": entity.name, **entity.properties}
        rows = self._run(
            q.UPSERT_ENTITY[entity.kind],
            {
                "id": entity.id,
                "kind": entity.kind.value,
                "ts": _ts(entity.observed_at),
                "props": props,
            },
        )
        return WriteResult(id=entity.id, applied=bool(rows))

    def upsert_relationship(self, rel: RelationshipInput) -> WriteResult:
        ids = [rel.from_id, rel.to_id] + ([rel.source_id] if rel.source_id else [])
        self.require(ids)
        props: dict[str, Any] = dict(rel.properties)
        if rel.source_id:
            props["source_id"] = rel.source_id
        self._run(
            q.UPSERT_RELATIONSHIP[rel.type],
            {
                "from_id": rel.from_id,
                "to_id": rel.to_id,
                "ts": _ts(rel.observed_at),
                "props": props,
            },
        )
        return WriteResult(id=f"{rel.from_id}-[{rel.type.value}]->{rel.to_id}", applied=True)

    def register_source(self, source: SourceInput) -> WriteResult:
        properties: dict[str, Any] = {
            "source_type": source.source_type,
            "uri": source.uri,
            "excerpt": source.excerpt,
            "source_system": source.source_system,
            "source_record_id": source.source_record_id,
            "published_at": source.published_at,
            "status": "active",
        }
        result = self.upsert_entity(
            EntityInput(
                id=source.id,
                name=source.name,
                properties={k: v for k, v in properties.items() if v is not None},
                observed_at=source.observed_at,
            )
        )
        links: list[tuple[Rel, str, str]] = (
            [(Rel.SUPPORTED_BY, target, source.id) for target in source.supports]
            + [(Rel.MENTIONS, source.id, target) for target in source.mentions]
            + [(Rel.CONTRADICTS, source.id, target) for target in source.contradicts]
            + [(Rel.AUTHORED_BY, source.id, author) for author in source.authored_by]
        )
        self.require([a for _, a, _ in links] + [b for _, _, b in links])
        for rel, from_id, to_id in links:
            self.upsert_relationship(
                RelationshipInput(
                    type=rel,
                    from_id=from_id,
                    to_id=to_id,
                    observed_at=source.observed_at,
                )
            )
        return result

    # ------------------------------------------------------------ events

    def apply_event(self, event: Event) -> EventResult:
        handler = {
            "ticket_status_changed": self._ticket_status_changed,
            "ownership_changed": self._ownership_changed,
            "incident_recorded": self._incident_recorded,
            "decision_superseded": self._decision_superseded,
            "source_withdrawn": self._source_withdrawn,
        }[event.type]
        changes = handler(event)  # type: ignore[operator]
        return EventResult(event_id=event.event_id, type=event.type, changes=changes)

    def _ticket_status_changed(self, event: TicketStatusChanged) -> list[str]:
        self.require([event.ticket_id] + ([event.source_id] if event.source_id else []))
        rows = self._run(
            q.SET_TICKET_STATUS,
            {
                "id": event.ticket_id,
                "status": event.status,
                "ts": _ts(event.occurred_at),
            },
        )
        if not rows:
            return [f"ignored: {event.ticket_id} already has a newer update"]
        changes = [f"{event.ticket_id} status {rows[0]['previous_status']} -> {event.status}"]
        if event.source_id:
            self.upsert_relationship(
                RelationshipInput(
                    type=Rel.SUPPORTED_BY,
                    from_id=event.ticket_id,
                    to_id=event.source_id,
                    observed_at=event.occurred_at,
                )
            )
            changes.append(f"{event.ticket_id} supported by {event.source_id}")
        return changes

    def _ownership_changed(self, event: OwnershipChanged) -> list[str]:
        ids = [event.entity_id, event.new_owner_id] + ([event.source_id] if event.source_id else [])
        self.require(ids)
        ts = _ts(event.occurred_at)
        closed = self._run(
            q.CLOSE_OTHER_OWNERS,
            {
                "entity_id": event.entity_id,
                "owner_id": event.new_owner_id,
                "owner_kind": kind_of(event.new_owner_id).value,
                "ts": ts,
                "event_id": event.event_id,
            },
        )
        self.upsert_relationship(
            RelationshipInput(
                type=Rel.OWNS,
                from_id=event.new_owner_id,
                to_id=event.entity_id,
                source_id=event.source_id,
                properties={"set_by_event": event.event_id},
                observed_at=event.occurred_at,
            )
        )
        changes = [f"{row['previous_owner_id']} no longer owns {event.entity_id}" for row in closed]
        changes.append(f"{event.new_owner_id} owns {event.entity_id}")
        return changes

    def _incident_recorded(self, event: IncidentRecorded) -> list[str]:
        self.require(event.affects + event.component_ids + event.source_ids)
        incident = event.incident.model_copy(
            update={"observed_at": event.incident.observed_at or event.occurred_at}
        )
        self.upsert_entity(incident)
        changes = [f"recorded {incident.id}"]
        links = (
            [(Rel.AFFECTS, incident.id, t) for t in event.affects]
            + [(Rel.IN_COMPONENT, incident.id, c) for c in event.component_ids]
            + [(Rel.SUPPORTED_BY, incident.id, s) for s in event.source_ids]
        )
        for rel, from_id, to_id in links:
            self.upsert_relationship(
                RelationshipInput(
                    type=rel,
                    from_id=from_id,
                    to_id=to_id,
                    observed_at=event.occurred_at,
                )
            )
            changes.append(f"{from_id} {rel.value} {to_id}")
        return changes

    def _decision_superseded(self, event: DecisionSuperseded) -> list[str]:
        self.upsert_relationship(
            RelationshipInput(
                type=Rel.SUPERSEDES,
                from_id=event.new_decision_id,
                to_id=event.old_decision_id,
                source_id=event.source_id,
                observed_at=event.occurred_at,
            )
        )
        return [f"{event.new_decision_id} supersedes {event.old_decision_id}"]

    def _source_withdrawn(self, event: SourceWithdrawn) -> list[str]:
        self.require([event.source_id])
        self._run(
            q.WITHDRAW_SOURCE,
            {
                "id": event.source_id,
                "reason": event.reason,
                "ts": _ts(event.occurred_at),
            },
        )
        return [f"{event.source_id} withdrawn: {event.reason}"]


__all__ = ["EntityNotFound", "Ingestor", "Kind"]
