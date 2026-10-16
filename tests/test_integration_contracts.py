"""Contract costing against a real FalkorDB: seeding, traversal, calculations, provenance."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from company_brain.api import create_app
from company_brain.config import Settings
from company_brain.contract_cost import (
    ContractCostInvestigator,
    ContractCostRequest,
    ContractCostResponse,
)
from company_brain.graph import CompanyNotFound, FalkorDBStore
from company_brain.ingest import EntityNotFound, Ingestor
from company_brain.models import EntityInput, RelationshipInput
from company_brain.schema import COST_KINDS, Rel, kind_of
from company_brain.seed import load_dataset, seed_contracts

AS_OF = date(2026, 10, 16)
LATER = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)


@pytest.fixture
def kaveri(real_store: FalkorDBStore, company: str) -> str:
    seed_contracts(real_store, company)
    return company


@pytest.fixture
def other_company(real_store: FalkorDBStore) -> Iterator[str]:
    company_id = f"test-{uuid.uuid4().hex[:10]}"
    yield company_id
    real_store.drop_company(company_id)


def _investigate(store: FalkorDBStore, company: str, ref: str = "CT-104") -> ContractCostResponse:
    return ContractCostInvestigator(store, company).investigate(
        ContractCostRequest(contract=ref, as_of=AS_OF)
    )


def _snapshot(raw_graph: Any, company: str) -> tuple[Any, Any]:
    graph = raw_graph(company)
    nodes = graph.ro_query("MATCH (n) RETURN n.id, properties(n) ORDER BY n.id").result_set
    edges = graph.ro_query(
        "MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id, properties(r) ORDER BY a.id, type(r), b.id"
    ).result_set
    return nodes, edges


# ---------------------------------------------------------------- seeding


def test_seed_is_idempotent_and_complete(
    real_store: FalkorDBStore, company: str, raw_graph: Any
) -> None:
    data = load_dataset("kaveri")
    seed_contracts(real_store, company)
    first = _snapshot(raw_graph, company)
    assert len(first[0]) == len(data["entities"]) + len(data["sources"])
    seed_contracts(real_store, company)
    assert _snapshot(raw_graph, company) == first
    labels = dict(
        raw_graph(company)
        .ro_query("MATCH (n:Entity) RETURN labels(n)[1], count(n) ORDER BY labels(n)[1]")
        .result_set
    )
    assert labels["Contract"] == 5 and labels["Job"] == 9 and labels["MachineryCost"] == 5
    charged = (
        raw_graph(company)
        .ro_query(
            "MATCH (:Purchase {id: 'purchase:pur-2047'})-[c:CHARGED_TO]->(j:Job) "
            "RETURN j.id, c.amount_paise, c.source_id ORDER BY j.id"
        )
        .result_set
    )
    assert charged == [
        ["job:j-104b", 4_600_000, "source:bill-cst-884"],
        ["job:j-104c", 4_600_000, "source:bill-cst-884"],
        ["job:j-105a", 4_600_000, "source:bill-cst-884"],
    ]


# ---------------------------------------------------------------- calculations


def test_contract_cost_and_provisional_profit(real_store: FalkorDBStore, kaveri: str) -> None:
    r = _investigate(real_store, kaveri)
    b, p = r.cost_breakdown, r.profitability
    assert (b.materials_paise, b.labour_paise, b.machinery_paise, b.other_paise) == (
        75_990_000,
        7_150_000,
        1_180_000,
        850_000,
    )
    assert b.total_paise == 85_170_000 and b.entries == 35
    assert sum(ln.charged_paise or 0 for ln in r.cost_lines) == b.total_paise
    assert p.contract_value_paise == 153_000_000
    assert p.provisional_profit_paise == 153_000_000 - 85_170_000
    assert p.provisional_margin_percent == "44.3"
    assert p.label.startswith("provisional")
    # graph totals agree with the itemised lines (no warning raised)
    assert not any("differ" in w for w in r.warnings)
    assert r.queries_executed[:4] == [
        "mf_contract",
        "mf_contract_charges",
        "mf_contract_cost_totals",
        "mf_contract_noted",
    ]


def test_void_entries_are_never_counted(real_store: FalkorDBStore, kaveri: str) -> None:
    r = _investigate(real_store, kaveri)
    assert r.cost_breakdown.excluded_void == ["PUR-2056"]
    assert "PUR-2056" not in {ln.ref for ln in r.cost_lines}
    assert {ln.ref for ln in r.cost_lines if ln.ref == "PUR-2055"} == {"PUR-2055"}


def test_shared_costs_are_split_not_copied(real_store: FalkorDBStore, kaveri: str) -> None:
    ct104 = _investigate(real_store, kaveri, "CT-104")
    ct105 = _investigate(real_store, kaveri, "CT-105")
    tube_104 = sum(ln.charged_paise or 0 for ln in ct104.cost_lines if ln.ref == "PUR-2047")
    tube_105 = sum(ln.charged_paise or 0 for ln in ct105.cost_lines if ln.ref == "PUR-2047")
    assert (tube_104, tube_105) == (9_200_000, 4_600_000)
    assert tube_104 + tube_105 == 13_800_000
    assert any(f.category == "shared_cost" and "PUR-2047" in f.statement for f in ct104.findings)


def test_every_cost_is_counted_exactly_once(real_store: FalkorDBStore, kaveri: str) -> None:
    """Contract costs + unallocated costs = all valid recorded costs. Nothing twice."""
    data = load_dataset("kaveri")
    costs = {k.value for k in COST_KINDS}
    recorded = sum(
        e["properties"]["amount_paise"]
        for e in data["entities"]
        if kind_of(e["id"]).value in costs and e["properties"].get("status") != "void"
    )
    investigator = ContractCostInvestigator(real_store, kaveri)
    in_contracts = sum(c.recorded_cost_paise for c in investigator.overview().contracts)
    unallocated = investigator.unallocated().total_unallocated_paise
    assert in_contracts + unallocated == recorded


def test_payments_settle_bills_but_never_add_cost(real_store: FalkorDBStore, kaveri: str) -> None:
    before = _investigate(real_store, kaveri)
    bill = next(b for b in before.supplier_bills if b.ref == "PUR-2042")
    assert (bill.bill_amount_paise, bill.paid_cleared_paise, bill.outstanding_paise) == (
        16_740_000,
        10_000_000,
        6_740_000,
    )
    ingest = Ingestor(real_store, kaveri)
    ingest.upsert_entity(
        EntityInput(
            id="payment:sp-399",
            name="Supplier payment (NEFT)",
            properties={"status": "cleared", "amount_paise": 6_740_000, "paid_on": "2026-10-16"},
            observed_at=LATER,
        )
    )
    ingest.upsert_relationship(
        RelationshipInput(
            type=Rel.ALLOCATED_TO,
            from_id="payment:sp-399",
            to_id="purchase:pur-2042",
            properties={"amount_paise": 6_740_000},
            observed_at=LATER,
        )
    )
    after = _investigate(real_store, kaveri)
    assert after.cost_breakdown.total_paise == before.cost_breakdown.total_paise
    settled = next(b for b in after.supplier_bills if b.ref == "PUR-2042")
    assert settled.outstanding_paise == 0


def test_customer_receipts_are_not_revenue(real_store: FalkorDBStore, kaveri: str) -> None:
    r = _investigate(real_store, kaveri)
    a = r.customer_account
    assert (a.contract_value_paise, a.received_paise, a.outstanding_paise) == (
        153_000_000,
        80_000_000,
        73_000_000,
    )
    assert r.profitability.contract_value_paise == 153_000_000  # receipts did not add to it
    ct101 = _investigate(real_store, kaveri, "CT-101")
    assert ct101.customer_account.outstanding_paise == 0
    assert ct101.profitability.provisional_profit_paise == 11_000_000


# ---------------------------------------------------------------- uncertainty


def test_unknowns_decisions_and_inconsistencies(real_store: FalkorDBStore, kaveri: str) -> None:
    r = _investigate(real_store, kaveri)
    by_category = {f.category: f for f in r.findings}
    assert by_category["payment_unconfirmed"].kind == "unknown"
    assert "SP-307" in by_category["payment_unconfirmed"].statement
    assert by_category["unreconciled"].kind == "inconsistency"
    assert "PUR-2058" in by_category["unreconciled"].statement
    assert by_category["unapplied_payment"].kind == "decision"
    assert "SP-309" in by_category["unapplied_payment"].statement
    assert by_category["unassigned_labour"].kind == "unknown"
    assert "WG-416" in by_category["unassigned_labour"].statement
    assert by_category["wage_payment_unknown"].kind == "unknown"
    assert "WG-417" in by_category["wage_payment_unknown"].statement
    pending = {p.ref: p for p in r.pending_allocations}
    assert set(pending) == {"PUR-2053", "WG-413"}
    assert r.profitability.pending_allocation_paise == 2_850_000 + 360_000
    assert r.profitability.profit_if_all_pending_charged_here_paise == (
        r.profitability.provisional_profit_paise - 3_210_000
    )
    overheads = by_category["unallocated_overheads"]
    assert "₹39,790" in overheads.statement
    assert any("MC-404" in m for m in r.missing_information)
    bills = {b.ref: b for b in r.supplier_bills}
    assert bills["PUR-2055"].reconciliation == "unconfirmed"
    assert bills["PUR-2058"].reconciliation == "mismatch"
    assert bills["EX-401"].outstanding_paise == 0  # paid in cash per the expense book
    assert {w["ref"] for w in r.wages_unpaid} == {"WG-414", "WG-415"}
    assert any("not statutory" in w for w in r.warnings)


def test_charges_without_amount_and_over_allocation(
    real_store: FalkorDBStore, company: str
) -> None:
    ingest = Ingestor(real_store, company)
    for entity in [
        EntityInput(id="customer:c1", name="C1"),
        EntityInput(id="contract:k1", name="K1", properties={"amount_paise": 100_000}),
        EntityInput(id="job:k1a", name="K1A"),
        EntityInput(id="purchase:p1", name="P1", properties={"amount_paise": 10_000}),
        EntityInput(id="purchase:p2", name="P2", properties={"amount_paise": 5_000}),
    ]:
        ingest.upsert_entity(entity)
    for rel_type, a, b, props in [
        (Rel.PLACED_BY, "contract:k1", "customer:c1", {}),
        (Rel.PART_OF, "job:k1a", "contract:k1", {}),
        (Rel.CHARGED_TO, "purchase:p1", "job:k1a", {"amount_paise": 8_000}),
        (Rel.CHARGED_TO, "purchase:p1", "contract:k1", {"amount_paise": 4_000}),  # 12k > 10k
        (Rel.CHARGED_TO, "purchase:p2", "job:k1a", {}),  # no amount recorded
    ]:
        ingest.upsert_relationship(
            RelationshipInput(type=rel_type, from_id=a, to_id=b, properties=props)
        )
    r = _investigate(real_store, company, "K1")
    assert r.cost_breakdown.total_paise == 12_000  # p1 as charged; p2 excluded, not zero-filled
    # entries without a business reference fall back to their graph id
    assert r.cost_breakdown.excluded_without_amount == ["purchase:p2"]
    kinds = {f.category: f.kind for f in r.findings}
    assert kinds["charge_without_amount"] == "unknown"
    assert kinds["over_allocated"] == "inconsistency"
    assert any("No source record supports contract" in m for m in r.missing_information)


# ---------------------------------------------------------------- provenance


def test_cost_lines_carry_paths_and_sources(real_store: FalkorDBStore, kaveri: str) -> None:
    r = _investigate(real_store, kaveri)
    paths = {p.id: p for p in r.evidence_paths}
    for line in r.cost_lines:
        assert line.source_ids, line.ref
        assert line.path_id in paths
    tube = next(ln for ln in r.cost_lines if ln.ref == "PUR-2047" and ln.charged_to == "job:j-104b")
    edges = {(e.type, e.from_id, e.to_id, e.source_id) for e in paths[tube.path_id].edges}
    assert ("PART_OF", "job:j-104b", "contract:ct-104", "source:crg-p2") in edges
    assert ("CHARGED_TO", "purchase:pur-2047", "job:j-104b", "source:bill-cst-884") in edges
    assert ("SUPPLIED_BY", "purchase:pur-2047", "supplier:sup-04", None) in edges
    cited = {s.id for s in r.sources}
    for f in r.findings:
        assert set(f.source_ids) <= cited, f.category
    assert tube.shared_with == ["job:j-105a"]


# ---------------------------------------------------------------- updates and isolation


def test_new_records_change_the_investigation(real_store: FalkorDBStore, kaveri: str) -> None:
    before = _investigate(real_store, kaveri)
    seed_contracts(real_store, kaveri, dataset="kaveri-updates")
    seed_contracts(real_store, kaveri, dataset="kaveri-updates")  # replay is a no-op
    after = _investigate(real_store, kaveri)
    assert after.cost_breakdown.total_paise == before.cost_breakdown.total_paise + 1_900_000 + (
        360_000 + 648_000
    )
    assert {p.ref for p in after.pending_allocations} == {"WG-413"}
    assert not any(f.category == "payment_unconfirmed" for f in after.findings)
    changes = ContractCostInvestigator(real_store, kaveri).changed_since("2026-10-15T18:00:00Z")
    assert {(c["change"], c["ref"]) for c in changes} == {
        ("record", "SP-307"),
        ("record", "PUR-2063"),
        ("allocation", "PUR-2053"),
        ("allocation", "WG-416"),
        ("allocation", "PUR-2063"),
    }


def test_companies_are_isolated(real_store: FalkorDBStore, kaveri: str, other_company: str) -> None:
    with pytest.raises(CompanyNotFound):
        _investigate(real_store, other_company)
    seed_contracts(real_store, other_company)
    Ingestor(real_store, other_company).upsert_entity(
        EntityInput(
            id="contract:ct-104",
            name="other",
            properties={"amount_paise": 1},
            observed_at=LATER,
        )
    )
    assert _investigate(real_store, kaveri).profitability.contract_value_paise == 153_000_000
    assert _investigate(real_store, other_company).profitability.contract_value_paise == 1


def test_unknown_contract(real_store: FalkorDBStore, kaveri: str) -> None:
    with pytest.raises(EntityNotFound):
        _investigate(real_store, kaveri, "CT-999")


def test_http_contract_endpoints(settings: Settings, kaveri: str, other_company: str) -> None:
    key = "test-key-contracts-012345"
    app = create_app(
        settings.model_copy(update={"company_brain_api_keys": SecretStr(f"{key}={kaveri}")})
    )
    client = TestClient(app)
    client.headers["X-API-Key"] = key
    base = f"/v1/companies/{kaveri}"
    response = client.post(
        f"{base}/contracts/investigations", json={"contract": "CT-104", "as_of": "2026-10-16"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profitability"]["provisional_profit_paise"] == 67_830_000
    assert {"type", "from", "to", "source_id"} <= set(body["evidence_paths"][0]["edges"][0])
    assert client.get(f"{base}/contracts").status_code == 200
    assert client.get(f"{base}/costs/unallocated").json()["total_unallocated_paise"] == 7_549_000
    changes = client.get(f"{base}/costs/changes", params={"since": "2026-10-15T17:00:00Z"})
    assert changes.status_code == 200 and changes.json()
    assert (
        client.post(f"{base}/contracts/investigations", json={"contract": "CT-999"}).status_code
        == 404
    )
    denied = client.get(f"/v1/companies/{other_company}/contracts")
    assert denied.status_code == 403
