"""Typed request and response models for the HTTP API and services."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from company_brain.schema import (
    COMMON_PROPERTIES,
    KIND_PROPERTIES,
    SOURCE_TYPES,
    STATUS_VALUES,
    Kind,
    Rel,
    check_relationship,
    kind_of,
)

PropertyValue = str | int | float | bool | None
EntityId = Annotated[str, Field(min_length=3, max_length=120, examples=["ticket:atlas-142"])]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    """Timestamps are stored as fixed-format UTC strings so they compare lexically."""
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityInput(_Strict):
    """Create or update one entity. The kind is taken from the id prefix."""

    id: EntityId
    name: str = Field(min_length=1, max_length=300)
    properties: dict[str, PropertyValue] = Field(default_factory=dict)
    observed_at: datetime | None = Field(
        default=None,
        description="When this version of the record was true. Older writes never "
        "overwrite newer ones. Defaults to now.",
    )

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        kind_of(value)
        return value

    @property
    def kind(self) -> Kind:
        return kind_of(self.id)

    @model_validator(mode="after")
    def _check_properties(self) -> EntityInput:
        kind = self.kind
        allowed = COMMON_PROPERTIES | KIND_PROPERTIES[kind]
        unknown = set(self.properties) - allowed
        if unknown:
            raise ValueError(
                f"properties {sorted(unknown)} are not allowed for {kind.value}; "
                f"allowed: {sorted(allowed - {'name'})}"
            )
        if "name" in self.properties:
            raise ValueError("set 'name' at the top level, not in properties")
        status = self.properties.get("status")
        if status is not None and kind in STATUS_VALUES and status not in STATUS_VALUES[kind]:
            raise ValueError(
                f"status {status!r} is not valid for {kind.value}; "
                f"use one of {sorted(STATUS_VALUES[kind])}"
            )
        for key in ("due_date",):
            if key in self.properties and self.properties[key] is not None:
                date.fromisoformat(str(self.properties[key]))
        if kind is Kind.SOURCE:
            stype = self.properties.get("source_type")
            if stype is not None and stype not in SOURCE_TYPES:
                raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
        for value in self.properties.values():
            if isinstance(value, str) and len(value) > 4000:
                raise ValueError("property values are limited to 4000 characters")
        return self


class RelationshipInput(_Strict):
    """Create or update one allowed relationship between two existing entities."""

    type: Rel
    from_id: EntityId
    to_id: EntityId
    source_id: EntityId | None = Field(
        default=None, description="Source record that asserts this relationship."
    )
    properties: dict[str, PropertyValue] = Field(default_factory=dict, max_length=20)
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> RelationshipInput:
        check_relationship(self.type, kind_of(self.from_id), kind_of(self.to_id))
        if self.source_id is not None and kind_of(self.source_id) is not Kind.SOURCE:
            raise ValueError("source_id must reference a source entity")
        reserved = {"source_id", "created_at", "updated_at", "valid_from", "valid_to"}
        if reserved & set(self.properties):
            raise ValueError(f"properties may not set {sorted(reserved)}")
        return self


class SourceInput(_Strict):
    """Register a source record and what it supports, mentions or contradicts."""

    id: EntityId
    name: str = Field(min_length=1, max_length=300, description="Title of the source.")
    source_type: str = Field(examples=["postmortem"])
    uri: str | None = Field(default=None, max_length=2000)
    excerpt: str | None = Field(default=None, max_length=4000)
    source_system: str | None = Field(default=None, max_length=100, examples=["confluence"])
    source_record_id: str | None = Field(default=None, max_length=200)
    published_at: str | None = None
    authored_by: list[EntityId] = Field(default_factory=list, max_length=20)
    supports: list[EntityId] = Field(
        default_factory=list,
        max_length=50,
        description="Decisions, incidents or tickets this source is evidence for.",
    )
    mentions: list[EntityId] = Field(
        default_factory=list,
        max_length=50,
        description="Entities referenced by the source without it being evidence for them.",
    )
    contradicts: list[EntityId] = Field(default_factory=list, max_length=50)
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> SourceInput:
        if kind_of(self.id) is not Kind.SOURCE:
            raise ValueError("source ids must start with 'source:'")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
        for target in self.supports:
            check_relationship(Rel.SUPPORTED_BY, kind_of(target), Kind.SOURCE)
        for target in self.mentions:
            check_relationship(Rel.MENTIONS, Kind.SOURCE, kind_of(target))
        for target in self.contradicts:
            check_relationship(Rel.CONTRADICTS, Kind.SOURCE, kind_of(target))
        for author in self.authored_by:
            check_relationship(Rel.AUTHORED_BY, Kind.SOURCE, kind_of(author))
        return self


# ---------------------------------------------------------------- events


class _Event(_Strict):
    event_id: str = Field(min_length=1, max_length=120, examples=["evt-2026-10-16-001"])
    occurred_at: datetime | None = None


class TicketStatusChanged(_Event):
    type: Literal["ticket_status_changed"]
    ticket_id: EntityId
    status: str
    source_id: EntityId | None = None

    @model_validator(mode="after")
    def _check(self) -> TicketStatusChanged:
        if kind_of(self.ticket_id) is not Kind.TICKET:
            raise ValueError("ticket_id must reference a ticket")
        if self.status not in STATUS_VALUES[Kind.TICKET]:
            raise ValueError(f"status must be one of {sorted(STATUS_VALUES[Kind.TICKET])}")
        return self


class OwnershipChanged(_Event):
    """Replace the current owner of the same kind (person or team) with a new one."""

    type: Literal["ownership_changed"]
    entity_id: EntityId
    new_owner_id: EntityId
    source_id: EntityId | None = None

    @model_validator(mode="after")
    def _check(self) -> OwnershipChanged:
        check_relationship(Rel.OWNS, kind_of(self.new_owner_id), kind_of(self.entity_id))
        return self


class IncidentRecorded(_Event):
    type: Literal["incident_recorded"]
    incident: EntityInput
    affects: list[EntityId] = Field(default_factory=list, max_length=50)
    component_ids: list[EntityId] = Field(default_factory=list, max_length=20)
    source_ids: list[EntityId] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _check(self) -> IncidentRecorded:
        if self.incident.kind is not Kind.INCIDENT:
            raise ValueError("incident.id must reference an incident")
        for target in self.affects:
            check_relationship(Rel.AFFECTS, Kind.INCIDENT, kind_of(target))
        for cid in self.component_ids:
            check_relationship(Rel.IN_COMPONENT, Kind.INCIDENT, kind_of(cid))
        for sid in self.source_ids:
            check_relationship(Rel.SUPPORTED_BY, Kind.INCIDENT, kind_of(sid))
        return self


class DecisionSuperseded(_Event):
    type: Literal["decision_superseded"]
    new_decision_id: EntityId
    old_decision_id: EntityId
    source_id: EntityId | None = None

    @model_validator(mode="after")
    def _check(self) -> DecisionSuperseded:
        check_relationship(
            Rel.SUPERSEDES, kind_of(self.new_decision_id), kind_of(self.old_decision_id)
        )
        if self.new_decision_id == self.old_decision_id:
            raise ValueError("a decision cannot supersede itself")
        return self


class SourceWithdrawn(_Event):
    type: Literal["source_withdrawn"]
    source_id: EntityId
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> SourceWithdrawn:
        if kind_of(self.source_id) is not Kind.SOURCE:
            raise ValueError("source_id must reference a source")
        return self


Event = Annotated[
    TicketStatusChanged
    | OwnershipChanged
    | IncidentRecorded
    | DecisionSuperseded
    | SourceWithdrawn,
    Field(discriminator="type"),
]


class EventResult(BaseModel):
    event_id: str
    type: str
    changes: list[str]


class WriteResult(BaseModel):
    id: str
    applied: bool = Field(
        description="False when the write was ignored because a newer version already exists."
    )


# ---------------------------------------------------------------- investigation


class InvestigationRequest(_Strict):
    question: str = Field(
        min_length=3,
        max_length=1000,
        examples=["Why is Project Atlas behind schedule and who owns the blockers?"],
    )
    focus_ids: list[EntityId] = Field(
        default_factory=list,
        max_length=5,
        description="Entities to investigate. If empty, entities are matched from the question.",
    )
    as_of: date | None = Field(
        default=None,
        description="Date used to decide whether work is overdue. Defaults to today.",
    )

    @field_validator("focus_ids")
    @classmethod
    def _check_focus(cls, ids: list[str]) -> list[str]:
        for eid in ids:
            if kind_of(eid) not in {Kind.PROJECT, Kind.COMPONENT, Kind.TICKET}:
                raise ValueError("focus_ids may reference projects, components or tickets")
        return ids


class EntityRef(BaseModel):
    id: str
    kind: str
    name: str | None = None
    match_reason: str | None = None


class PathEdge(BaseModel):
    type: str
    from_id: str = Field(serialization_alias="from")
    to_id: str = Field(serialization_alias="to")
    source_id: str | None = Field(
        default=None, description="Source record that asserted this relationship, if recorded."
    )


class EvidencePath(BaseModel):
    id: str
    description: str
    edges: list[PathEdge]


class SourceRef(BaseModel):
    id: str
    name: str | None = None
    uri: str | None = None
    source_type: str | None = None
    status: str = "active"
    excerpt: str | None = None
    source_system: str | None = None
    source_record_id: str | None = None
    authors: list[str] = []
    relation: str
    about: str


class Finding(BaseModel):
    id: str
    category: Literal[
        "blocked_work",
        "overdue_work",
        "blocker_dependency",
        "incident",
        "ownership",
        "expertise",
        "current_decision",
        "superseded_decision",
    ]
    statement: str = Field(description="Deterministic statement rendered from graph facts.")
    entity_ids: list[str]
    path_ids: list[str] = []
    source_ids: list[str] = []
    supported: bool = Field(
        description="True when at least one active source record backs this finding. False "
        "means the fact exists in the graph but no active source is attached to it."
    )


class Owner(BaseModel):
    entity_id: str
    owner_id: str
    owner_kind: str
    owner_name: str | None = None
    owner_teams: list[str] = []
    source_id: str | None = Field(
        default=None, description="Source record that asserted this ownership, if recorded."
    )


class Expert(BaseModel):
    person_id: str
    name: str | None = None
    score: int
    basis: list[dict[str, str]]


class DecisionRef(BaseModel):
    id: str
    name: str | None = None
    status: str | None = None
    decided_at: str | None = None
    decided_by: list[dict[str, Any]] = []
    affects: list[str] = []
    supersedes: list[str] = []
    superseded_by: list[str] = []
    current: bool


class InvestigationResponse(BaseModel):
    investigation_id: str
    company_id: str
    question: str
    as_of: date
    matched_entities: list[EntityRef]
    findings: list[Finding]
    evidence_paths: list[EvidencePath]
    sources: list[SourceRef]
    current_owners: list[Owner]
    experts: list[Expert]
    decisions: list[DecisionRef]
    missing_information: list[str]
    warnings: list[str]
    queries_executed: list[str] = Field(
        description="Names of the fixed graph queries that produced this result."
    )
