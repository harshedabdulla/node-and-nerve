"""Contract costing without a database: dataset coherence, calculation rules, API errors."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from company_brain.api import create_app
from company_brain.config import Settings
from company_brain.contract_cost import ContractCostInvestigator, contract_id, percent
from company_brain.graph import GraphUnavailable, Query, Row
from company_brain.models import EntityInput, RelationshipInput
from company_brain.schema import COST_KINDS, Kind, Rel, kind_of
from company_brain.seed import load_dataset
from tests.conftest import FakeStore

DATA = load_dataset("kaveri")
COSTS = {k.value for k in COST_KINDS}


def _entities() -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in DATA["entities"]}


def _rels(rel_type: str) -> list[dict[str, Any]]:
    return [r for r in DATA["relationships"] if r["type"] == rel_type]


# ---------------------------------------------------------------- dataset coherence


def test_dataset_is_synthetic_and_scenarios_are_documented() -> None:
    assert "fictional" in DATA["_comment"] and "synthetic" in DATA["_comment"]
    assert len(DATA["_scenarios"]) >= 10
    kinds = defaultdict(int)
    for e in DATA["entities"]:
        kinds[kind_of(e["id"]).value] += 1
    assert kinds["contract"] == 5 and kinds["job"] == 9 and kinds["customer"] == 4
    assert kinds["supplier"] == 7 and kinds["worker"] == 6
    assert kinds["purchase"] >= 20 and kinds["wage"] >= 30
    assert kinds["machinerycost"] >= 4 and kinds["expense"] >= 4


def test_every_record_passes_model_validation() -> None:
    for e in DATA["entities"]:
        EntityInput(**e)
    for r in DATA["relationships"]:
        RelationshipInput(
            type=r["type"],
            from_id=r["from"],
            to_id=r["to"],
            source_id=r.get("source_id"),
            properties=r.get("properties", {}),
        )


def test_purchase_amounts_equal_quantity_times_unit_price() -> None:
    for e in _entities().values():
        p = e["properties"]
        if kind_of(e["id"]) is Kind.PURCHASE:
            assert round(p["quantity"] * p["unit_price_paise"]) == p["amount_paise"], e["id"]


def test_allocations_never_exceed_the_record_and_full_splits_add_up() -> None:
    ents = _entities()
    charged: dict[str, int] = defaultdict(int)
    for r in _rels("CHARGED_TO"):
        assert isinstance(r["properties"]["amount_paise"], int)
        charged[r["from"]] += r["properties"]["amount_paise"]
    for rid, total in charged.items():
        assert total <= ents[rid]["properties"]["amount_paise"], rid
    # the shared square tube is split exactly across two contracts
    tube = [r for r in _rels("CHARGED_TO") if r["from"] == "purchase:pur-2047"]
    assert sorted(r["to"] for r in tube) == ["job:j-104b", "job:j-104c", "job:j-105a"]
    assert sum(r["properties"]["amount_paise"] for r in tube) == 13_800_000


def test_payments_never_exceed_what_they_settle() -> None:
    ents = _entities()
    applied: dict[str, int] = defaultdict(int)
    for r in _rels("ALLOCATED_TO"):
        applied[r["from"]] += r["properties"]["amount_paise"]
        assert kind_of(r["to"]) in {Kind.PURCHASE, Kind.MACHINERY_COST, Kind.EXPENSE, Kind.CONTRACT}
    for pid, total in applied.items():
        assert total <= ents[pid]["properties"]["amount_paise"], pid


def test_every_cost_record_has_a_source_except_the_documented_slip() -> None:
    supported = {s for src in DATA["sources"] for s in src["supports"]}
    missing = sorted(
        e["properties"]["source_record_id"]
        for e in DATA["entities"]
        if kind_of(e["id"]).value in COSTS and e["id"] not in supported
    )
    assert missing == ["MC-404"]


def test_deliberate_data_quality_cases_are_present() -> None:
    ents = _entities()
    assert ents["purchase:pur-2056"]["properties"]["status"] == "void"
    noted = {r["from"] for r in _rels("NOTED_FOR")}
    assert noted == {"purchase:pur-2053", "wage:wg-413"}
    charged = {r["from"] for r in _rels("CHARGED_TO")}
    assert "wage:wg-416" not in charged and "wage:wg-416" not in noted
    assert "paid_status" not in ents["wage:wg-417"]["properties"]
    assert ents["payment:sp-307"]["properties"]["status"] == "pending"
    applied = {r["from"] for r in _rels("ALLOCATED_TO")}
    assert "payment:sp-309" not in applied


# ---------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("CT-104", "contract:ct-104"),
        ("contract:ct-104", "contract:ct-104"),
        (" ct-105 ", "contract:ct-105"),
    ],
)
def test_contract_references_are_normalised(ref: str, expected: str) -> None:
    assert contract_id(ref) == expected


@pytest.mark.parametrize("ref", ["", "CT 104", "job:j-104a", "CT-104; MATCH (n) DELETE n"])
def test_bad_contract_references_are_rejected(ref: str) -> None:
    with pytest.raises(ValueError):
        contract_id(ref)


def test_percent_is_exact_decimal() -> None:
    assert percent(67_830_000, 153_000_000) == "44.3"
    assert percent(1, 3) == "33.3"
    assert percent(2, 3) == "66.7"
    assert percent(5, 0) is None


@pytest.mark.parametrize(
    ("kind", "book", "amount", "cleared", "pending", "expected"),
    [
        ("purchase", "paid", 100, 100, 0, "consistent"),
        ("purchase", "paid", 100, 0, 100, "unconfirmed"),
        ("purchase", "paid", 100, 0, 0, "mismatch"),
        ("purchase", "unpaid", 100, 0, 0, "consistent"),
        ("purchase", "unpaid", 100, 40, 0, "mismatch"),
        ("purchase", "partly_paid", 100, 40, 0, "consistent"),
        ("purchase", "partly_paid", 100, 100, 0, "mismatch"),
        ("purchase", None, 100, 0, 0, "not_recorded"),
        ("expense", "paid", 100, 0, 0, "consistent"),
        ("expense", None, 100, 0, 0, "not_recorded"),
    ],
)
def test_book_payment_status_is_reconciled_against_payment_records(
    kind: str, book: str | None, amount: int, cleared: int, pending: int, expected: str
) -> None:
    assert ContractCostInvestigator._reconcile(kind, book, amount, cleared, pending) == expected


def test_costing_relationships_are_constrained() -> None:
    RelationshipInput(type=Rel.CHARGED_TO, from_id="purchase:p", to_id="job:j")
    with pytest.raises(ValidationError, match="not allowed"):
        RelationshipInput(type=Rel.CHARGED_TO, from_id="payment:p", to_id="job:j")
    with pytest.raises(ValidationError, match="not allowed"):
        RelationshipInput(type=Rel.CHARGED_TO, from_id="purchase:p", to_id="customer:c")
    with pytest.raises(ValidationError, match="paid_status"):
        EntityInput(id="wage:w", name="W", properties={"paid_status": "maybe"})
    with pytest.raises(ValidationError, match="unit_price_paise"):
        EntityInput(id="purchase:p", name="P", properties={"unit_price_paise": 1.5})


# ---------------------------------------------------------------- API without a database

KEY = "key-kaveri-0123456789abc"


def _client(store: FakeStore) -> TestClient:
    settings = Settings(company_brain_api_keys=SecretStr(f"{KEY}=kaveri"))
    client = TestClient(create_app(settings, store_factory=lambda _: store))
    client.headers["X-API-Key"] = KEY
    return client


def test_contract_endpoints_are_company_scoped() -> None:
    store = FakeStore()
    client = _client(store)
    assert client.get("/v1/companies/acme/contracts").status_code == 403
    assert (
        client.post("/v1/companies/acme/contracts/investigations", json={"contract": "CT-1"})
    ).status_code == 403
    assert store.calls == []


def test_contract_investigation_errors() -> None:
    client = _client(FakeStore())
    bad = client.post("/v1/companies/kaveri/contracts/investigations", json={"contract": "CT 104"})
    assert bad.status_code == 422
    assert client.get("/v1/companies/kaveri/contracts").status_code == 404  # no graph yet
    store = FakeStore()
    store.companies.add("kaveri")
    missing = _client(store).post(
        "/v1/companies/kaveri/contracts/investigations", json={"contract": "CT-999"}
    )
    assert missing.status_code == 404


class _DownStore(FakeStore):
    def run(self, company_id: str, query: Query, params: dict[str, Any] | None = None) -> list[Row]:
        raise GraphUnavailable("down")

    def company_exists(self, company_id: str) -> bool:
        raise GraphUnavailable("down")


def test_database_unavailable_returns_503() -> None:
    client = _client(_DownStore())
    assert (
        client.post("/v1/companies/kaveri/contracts/investigations", json={"contract": "CT-104"})
    ).status_code == 503
    assert client.get("/v1/companies/kaveri/contracts").status_code == 503
    assert client.get("/v1/companies/kaveri/costs/unallocated").status_code == 503
