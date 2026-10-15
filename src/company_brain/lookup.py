"""Read operations: entity context, decision provenance and bounded connection discovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from company_brain import queries as q
from company_brain.graph import CompanyNotFound, GraphStore
from company_brain.ingest import EntityNotFound
from company_brain.models import PathEdge, SourceRef
from company_brain.schema import Kind, kind_of


class EntityView(BaseModel):
    entity: dict[str, Any]
    relationships: list[dict[str, Any]]


class DecisionProvenance(BaseModel):
    decision: dict[str, Any]
    current: bool
    decided_by: list[dict[str, Any]]
    affects: list[dict[str, Any]]
    superseded_by: list[str]
    supersedes_chains: list[list[str]]
    supporting_sources: list[SourceRef]
    mentioning_sources: list[SourceRef]
    contradicting_sources: list[SourceRef]
    related_work: list[str]
    warnings: list[str]


class Connection(BaseModel):
    hops: int
    nodes: list[dict[str, Any]]
    edges: list[PathEdge]


class Lookup:
    def __init__(self, store: GraphStore, company_id: str) -> None:
        self.store = store
        self.company_id = company_id

    def _require_company(self) -> None:
        if not self.store.company_exists(self.company_id):
            raise CompanyNotFound(self.company_id)

    def entity(self, entity_id: str) -> EntityView:
        kind_of(entity_id)
        self._require_company()
        rows = self.store.run(self.company_id, q.GET_ENTITY, {"id": entity_id})
        if not rows:
            raise EntityNotFound([entity_id])
        return EntityView(**rows[0])

    def decision_sources(self, decision_id: str) -> DecisionProvenance:
        if kind_of(decision_id) is not Kind.DECISION:
            raise ValueError("decision_id must reference a decision")
        self._require_company()
        rows = self.store.run(self.company_id, q.DECISION_PROVENANCE, {"id": decision_id})
        if not rows:
            raise EntityNotFound([decision_id])
        row = rows[0]
        sources = [
            SourceRef(relation=r["relation"], about=r["entity_id"], **r["source"])
            for r in self.store.run(self.company_id, q.SOURCES_FOR, {"ids": [decision_id]})
        ]
        sources.sort(key=lambda s: s.id)
        supporting = [s for s in sources if s.relation == "supports"]
        warnings = []
        if not any(s.status == "active" for s in supporting):
            warnings.append(f"{decision_id} has no active supporting source")
        warnings += [f"{s.id} is {s.status}" for s in supporting if s.status != "active"]
        accepted = (row["decision"].get("status") or "accepted") == "accepted"
        return DecisionProvenance(
            decision=row["decision"],
            current=accepted and not row["superseded_by"],
            decided_by=row["decided_by"],
            affects=sorted(row["affects"], key=lambda a: a["id"]),
            superseded_by=sorted(row["superseded_by"]),
            supersedes_chains=sorted(c for c in row["supersedes_chains"] if c),
            supporting_sources=supporting,
            mentioning_sources=[s for s in sources if s.relation == "mentions"],
            contradicting_sources=[s for s in sources if s.relation == "contradicts"],
            related_work=sorted(set(row["related_work"]) | set(row["co_mentioned_work"])),
            warnings=warnings,
        )

    def connections(
        self, from_id: str, to_id: str, max_depth: int = 3, limit: int = 5
    ) -> list[Connection]:
        kind_of(from_id)
        kind_of(to_id)
        if not 1 <= max_depth <= q.MAX_PATH_DEPTH:
            raise ValueError(f"max_depth must be between 1 and {q.MAX_PATH_DEPTH}")
        self._require_company()
        rows = self.store.run(
            self.company_id,
            q.CONNECTIONS[max_depth],
            {"from_id": from_id, "to_id": to_id, "limit": max(1, min(limit, 20))},
        )
        return [
            Connection(
                hops=r["hops"],
                nodes=r["nodes"],
                edges=[
                    PathEdge(
                        type=e["type"],
                        from_id=e["from"],
                        to_id=e["to"],
                        source_id=e.get("source_id"),
                    )
                    for e in r["rels"]
                ],
            )
            for r in rows
        ]
