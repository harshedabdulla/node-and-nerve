"""The graph data model: entity kinds, allowed relationships, and identifier rules.

This module is the single source of truth for what may be written to the graph.
Labels and relationship types can never come from caller input directly: FalkorDB
does not accept them as query parameters, so every label/type that appears in a
query is taken from the enums below.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Kind(StrEnum):
    PERSON = "person"
    TEAM = "team"
    PROJECT = "project"
    TICKET = "ticket"
    INCIDENT = "incident"
    DECISION = "decision"
    COMPONENT = "component"
    SOURCE = "source"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class Rel(StrEnum):
    MEMBER_OF = "MEMBER_OF"
    OWNS = "OWNS"
    PART_OF = "PART_OF"
    IN_COMPONENT = "IN_COMPONENT"
    BLOCKED_BY = "BLOCKED_BY"
    AFFECTS = "AFFECTS"
    RESOLVED = "RESOLVED"
    DECIDED_BY = "DECIDED_BY"
    SUPERSEDES = "SUPERSEDES"
    SUPPORTED_BY = "SUPPORTED_BY"
    MENTIONS = "MENTIONS"
    CONTRADICTS = "CONTRADICTS"
    AUTHORED_BY = "AUTHORED_BY"


K = Kind
_ANY: frozenset[Kind] = frozenset(Kind)

# relationship -> (allowed start kinds, allowed end kinds)
RELATIONSHIP_RULES: dict[Rel, tuple[frozenset[Kind], frozenset[Kind]]] = {
    Rel.MEMBER_OF: (frozenset({K.PERSON}), frozenset({K.TEAM})),
    Rel.OWNS: (
        frozenset({K.PERSON, K.TEAM}),
        frozenset({K.PROJECT, K.TICKET, K.COMPONENT}),
    ),
    Rel.PART_OF: (frozenset({K.TICKET, K.COMPONENT}), frozenset({K.PROJECT})),
    Rel.IN_COMPONENT: (frozenset({K.TICKET, K.INCIDENT}), frozenset({K.COMPONENT})),
    Rel.BLOCKED_BY: (
        frozenset({K.TICKET, K.PROJECT}),
        frozenset({K.TICKET, K.COMPONENT, K.INCIDENT}),
    ),
    Rel.AFFECTS: (
        frozenset({K.INCIDENT, K.DECISION}),
        frozenset({K.TICKET, K.PROJECT, K.COMPONENT}),
    ),
    Rel.RESOLVED: (frozenset({K.PERSON}), frozenset({K.INCIDENT})),
    Rel.DECIDED_BY: (frozenset({K.DECISION}), frozenset({K.PERSON})),
    Rel.SUPERSEDES: (frozenset({K.DECISION}), frozenset({K.DECISION})),
    Rel.SUPPORTED_BY: (
        frozenset({K.DECISION, K.INCIDENT, K.TICKET}),
        frozenset({K.SOURCE}),
    ),
    Rel.MENTIONS: (frozenset({K.SOURCE}), _ANY - {K.SOURCE}),
    Rel.CONTRADICTS: (frozenset({K.SOURCE}), frozenset({K.DECISION, K.INCIDENT})),
    Rel.AUTHORED_BY: (frozenset({K.SOURCE}), frozenset({K.PERSON})),
}

# Kind-specific properties a caller may set. Everything else is rejected.
COMMON_PROPERTIES = frozenset({"name", "description", "source_system", "source_record_id"})
KIND_PROPERTIES: dict[Kind, frozenset[str]] = {
    K.PERSON: frozenset({"title", "email_alias"}),
    K.TEAM: frozenset(),
    K.PROJECT: frozenset({"status", "due_date"}),
    K.TICKET: frozenset({"status", "due_date", "priority"}),
    K.INCIDENT: frozenset({"status", "severity", "occurred_at", "resolved_at"}),
    K.DECISION: frozenset({"status", "decided_at"}),
    K.COMPONENT: frozenset(),
    K.SOURCE: frozenset({"uri", "source_type", "excerpt", "status", "published_at"}),
}

STATUS_VALUES: dict[Kind, frozenset[str]] = {
    K.PROJECT: frozenset({"planned", "active", "at_risk", "done", "cancelled"}),
    K.TICKET: frozenset({"open", "in_progress", "blocked", "done", "cancelled"}),
    K.INCIDENT: frozenset({"open", "mitigated", "resolved"}),
    K.DECISION: frozenset({"proposed", "accepted", "rejected"}),
    K.SOURCE: frozenset({"active", "withdrawn"}),
}

SOURCE_TYPES = frozenset(
    {
        "document",
        "conversation",
        "ticket_record",
        "postmortem",
        "meeting_notes",
        "email",
        "other",
    }
)

ENTITY_ID_RE = re.compile(r"^(?P<kind>[a-z]+):(?P<slug>[a-z0-9][a-z0-9._-]{0,99})$")
COMPANY_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
GRAPH_PREFIX = "company_brain__"


def kind_of(entity_id: str) -> Kind:
    """Return the kind encoded in a stable entity id such as ``ticket:atlas-142``."""
    match = ENTITY_ID_RE.fullmatch(entity_id)
    if not match:
        raise ValueError(
            f"invalid entity id {entity_id!r}: expected '<kind>:<slug>' "
            "with a lowercase slug of letters, digits, '.', '_' or '-'"
        )
    try:
        return Kind(match["kind"])
    except ValueError:
        raise ValueError(f"unknown entity kind {match['kind']!r}") from None


def graph_name(company_id: str) -> str:
    """Derive the FalkorDB graph name for a company. The only way a graph is selected."""
    if not COMPANY_ID_RE.fullmatch(company_id):
        raise ValueError(
            f"invalid company id {company_id!r}: use 1-48 lowercase letters, digits or '-'"
        )
    return GRAPH_PREFIX + company_id.replace("-", "_")


def check_relationship(rel: Rel, from_kind: Kind, to_kind: Kind) -> None:
    starts, ends = RELATIONSHIP_RULES[rel]
    if from_kind not in starts or to_kind not in ends:
        raise ValueError(
            f"{rel.value} is not allowed from {from_kind.value} to {to_kind.value}; allowed: "
            f"{sorted(k.value for k in starts)} -> {sorted(k.value for k in ends)}"
        )
