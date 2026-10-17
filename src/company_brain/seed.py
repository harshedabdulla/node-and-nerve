"""Deterministic seed loaders for the synthetic datasets.

Northstar Systems (Project Atlas) is loaded from JSON. Varsha Electricals (Cashflow
Radar) is loaded from CSV files through the same importer customers use.

Every record uses a fixed ``observed_at`` timestamp and MERGE semantics, so running
a seed any number of times yields exactly the same nodes, edges and properties.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from typing import Any

from company_brain.csv_import import CsvImporter, ImportReport
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
            properties=r.get("properties", {}),
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


CASHFLOW_COMPANY = "varsha-electricals"
CASHFLOW_OBSERVED_AT = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
CASHFLOW_UPDATES_OBSERVED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def load_csv_dataset(name: str = "varsha") -> dict[str, str]:
    """CSV files of a packaged dataset as {file name: text}."""
    folder = resources.files("company_brain.data").joinpath(name)
    return {
        f.name: f.read_text("utf-8")
        for f in sorted(folder.iterdir(), key=lambda f: f.name)
        if f.name.endswith(".csv")
    }


def seed_cashflow(
    store: GraphStore,
    company_id: str = CASHFLOW_COMPANY,
    dataset: str = "varsha",
    observed_at: datetime = CASHFLOW_OBSERVED_AT,
) -> ImportReport:
    """Load the synthetic distributor through the CSV importer. Idempotent."""
    report = CsvImporter(store, company_id).run(
        load_csv_dataset(dataset), observed_at=observed_at, source_system="seed"
    )
    if not report.valid:
        raise ValueError(f"packaged dataset {dataset!r} is invalid: {report.errors}")
    return report


CONTRACTS_COMPANY = "kaveri-body-works"
CONTRACTS_AS_OF = date(2026, 10, 16)  # reporting date of the synthetic books


def seed_contracts(
    store: GraphStore, company_id: str = CONTRACTS_COMPANY, dataset: str = "kaveri"
) -> SeedSummary:
    """Load the synthetic lorry-body manufacturer (or its later updates). Idempotent."""
    return seed(store, load_dataset(dataset), company_id)
