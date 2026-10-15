"""Deterministic seed loader for the synthetic Northstar Systems graph.

Every record uses a fixed ``observed_at`` timestamp and MERGE semantics, so running
the seed any number of times yields exactly the same nodes, edges and properties.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

from company_brain.graph import GraphStore
from company_brain.ingest import Ingestor
from company_brain.models import EntityInput, RelationshipInput, SourceInput


@dataclass
class SeedSummary:
    company_id: str
    entities: int
    sources: int
    relationships: int


def load_dataset(name: str = "northstar") -> dict[str, Any]:
    text = resources.files("company_brain.data").joinpath(f"{name}.json").read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def seed(store: GraphStore, dataset: dict[str, Any], company_id: str | None = None) -> SeedSummary:
    company = company_id or dataset["company_id"]
    observed = datetime.fromisoformat(dataset["observed_at"])
    ingest = Ingestor(store, company)
    # Validate everything before writing anything.
    entities = [EntityInput(observed_at=observed, **e) for e in dataset["entities"]]
    sources = [SourceInput(observed_at=observed, **s) for s in dataset["sources"]]
    relationships = [
        RelationshipInput(
            type=r["type"],
            from_id=r["from"],
            to_id=r["to"],
            source_id=r.get("source_id"),
            observed_at=observed,
        )
        for r in dataset["relationships"]
    ]
    for entity in entities:
        ingest.upsert_entity(entity)
    for source in sources:
        ingest.register_source(source)
    for rel in relationships:
        ingest.upsert_relationship(rel)
    return SeedSummary(company, len(entities), len(sources), len(relationships))
