"""Cashflow Radar against a real FalkorDB: import, graph traversal, provenance, isolation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from company_brain.api import create_app
from company_brain.cashflow import CashflowInvestigator, CashflowRequest, CashflowResponse
from company_brain.config import Settings
from company_brain.csv_import import CsvImporter
from company_brain.graph import CompanyNotFound, FalkorDBStore
from company_brain.ingest import Ingestor
from company_brain.models import SourceWithdrawn
from company_brain.seed import (
    CASHFLOW_UPDATES_OBSERVED_AT,
    load_csv_dataset,
    seed_cashflow,
)

AS_OF = date(2026, 10, 16)


@pytest.fixture
def varsha(real_store: FalkorDBStore, company: str) -> str:
    seed_cashflow(real_store, company)
    return company


@pytest.fixture
def other_company(real_store: FalkorDBStore) -> Iterator[str]:
    company_id = f"test-{uuid.uuid4().hex[:10]}"
    yield company_id
    real_store.drop_company(company_id)


def _investigate(store: FalkorDBStore, company: str, **kwargs: Any) -> CashflowResponse:
    return CashflowInvestigator(store, company).investigate(
        CashflowRequest(as_of=kwargs.pop("as_of", AS_OF), **kwargs)
    )


def _snapshot(raw_graph: Any, company: str) -> tuple[Any, Any]:
    graph = raw_graph(company)
    nodes = graph.ro_query("MATCH (n) RETURN n.id, properties(n) ORDER BY n.id").result_set
    edges = graph.ro_query(
        "MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id, properties(r) ORDER BY a.id, type(r), b.id"
    ).result_set
    return nodes, edges


# ---------------------------------------------------------------- import and graph shape


def test_seed_is_idempotent(real_store: FalkorDBStore, company: str, raw_graph: Any) -> None:
    first = seed_cashflow(real_store, company)
    assert first.valid and first.written
    snapshot = _snapshot(raw_graph, company)
    assert len(snapshot[0]) == 43 and len(snapshot[1]) == 64
    second = seed_cashflow(real_store, company)
    assert second.written and second.ignored_stale == []
    assert _snapshot(raw_graph, company) == snapshot


def test_records_and_relationships_are_stored_with_provenance(varsha: str, raw_graph: Any) -> None:
    graph = raw_graph(varsha)
    labels = dict(
        graph.ro_query(
            "MATCH (n:Entity) RETURN labels(n)[1], count(n) ORDER BY labels(n)[1]"
        ).result_set
    )
    assert labels["Invoice"] == 6 and labels["SalesOrder"] == 5 and labels["PurchaseOrder"] == 3
    assert labels["Commitment"] == 7 and labels["Payment"] == 3 and labels["Source"] == 12
    inv = graph.ro_query(
        "MATCH (i:Invoice {id: 'invoice:inv-1001'}) RETURN properties(i)"
    ).result_set[0][0]
    assert inv["amount_paise"] == 47_200_000 and inv["currency"] == "INR"
    assert inv["source_record_id"] == "INV-1001" and inv["source_system"] == "seed:invoices.csv"
    dep = graph.ro_query(
        "MATCH (:Commitment {id: 'commitment:com-201'})-[d:DEPENDS_ON]->(i:Invoice) "
        "RETURN i.id, d.source_id"
    ).result_set
    assert dep == [["invoice:inv-1001", "source:src-007"]]
    alloc = graph.ro_query(
        "MATCH (:Payment {id: 'payment:pay-501'})-[a:ALLOCATED_TO]->(i) RETURN i.id, a.amount_paise"
    ).result_set
    assert alloc == [["invoice:inv-1001", 17_200_000]]


# ---------------------------------------------------------------- the investigation


def test_outstanding_balances_are_computed_from_cleared_payments(
    real_store: FalkorDBStore, varsha: str
) -> None:
    r = _investigate(real_store, varsha)
    rec = {x.ref: x for x in r.receivables}
    assert set(rec) == {"INV-1001", "INV-1002", "INV-1003", "INV-1004", "INV-1005"}
    assert "INV-0990" not in rec  # fully paid by a cleared payment
    assert (rec["INV-1001"].outstanding_paise, rec["INV-1001"].cleared_paise) == (
        30_000_000,
        17_200_000,
    )
    # a pending cheque is shown but not counted as received
    assert rec["INV-1003"].outstanding_paise == 8_500_000
    assert rec["INV-1003"].pending_paise == 8_500_000
    assert {x.ref: x.days_overdue for x in r.receivables if x.overdue} == {
        "INV-1001": 15,
        "INV-1002": 26,
        "INV-1003": 6,
    }
    assert not rec["INV-1005"].overdue and not rec["INV-1004"].overdue
    assert r.summary.outstanding_paise == 77_700_000
    assert r.summary.overdue_paise == 50_300_000


def test_documented_dependency_is_found_by_traversal_with_evidence(
    real_store: FalkorDBStore, varsha: str
) -> None:
    r = _investigate(real_store, varsha)
    deps = {(d.commitment_id, d.invoice_id): d for d in r.dependencies}
    com201 = deps[("commitment:com-201", "invoice:inv-1001")]
    assert com201.certainty == "documented" and com201.source_id == "source:src-007"
    path = next(p for p in r.evidence_paths if p.id == com201.path_id)
    edges = {(e.type, e.from_id, e.to_id) for e in path.edges}
    assert edges == {
        ("BILLED_TO", "invoice:inv-1001", "customer:cust-001"),
        ("DEPENDS_ON", "commitment:com-201", "invoice:inv-1001"),
        ("OWED_TO", "commitment:com-201", "supplier:sup-001"),
        ("FOR_ORDER", "commitment:com-201", "purchaseorder:po-301"),
        ("SOURCED_FROM", "salesorder:so-105", "purchaseorder:po-301"),
        ("PLACED_BY", "salesorder:so-105", "customer:cust-001"),
    }
    assert any(
        s.id == "source:src-007" and s.relation == "asserts" and "DEPENDS_ON" in s.about
        for s in r.sources
    )
    linked = [f for f in r.findings if f.category == "linked_order" and "SO-105" in f.statement]
    assert linked and "same customer that owes INV-1001" in linked[0].statement


def test_recorded_link_without_source_is_not_presented_as_documented(
    real_store: FalkorDBStore, varsha: str
) -> None:
    r = _investigate(real_store, varsha)
    com203 = next(d for d in r.dependencies if d.commitment_id == "commitment:com-203")
    assert com203.certainty == "recorded_without_source" and com203.source_id is None
    finding = next(
        f
        for f in r.findings
        if f.category == "collection_dependency" and "commitment:com-203" in f.entity_ids
    )
    assert finding.certainty == "recorded_without_source" and finding.source_ids == []
    assert any("COM-203 and INV-1004" in m for m in r.missing_information)
    assert r.summary.dependencies_without_source == 1


def test_no_dependency_is_invented(real_store: FalkorDBStore, varsha: str) -> None:
    r = _investigate(real_store, varsha)
    linked = {d.commitment_id for d in r.dependencies}
    # COM-202R is due this week and overdue invoices exist, but nothing links them
    assert "commitment:com-202r" not in linked
    assert any("COM-202R" in o and "not recorded" in o for o in r.open_questions)
    patil = next(x for x in r.receivables if x.ref == "INV-1002")
    assert patil.dependent_commitments == []
    assert any("INV-1002" in o for o in r.open_questions)
    assert {(d.commitment_id, d.invoice_id) for d in r.dependencies} == {
        ("commitment:com-201", "invoice:inv-1001"),
        ("commitment:com-203", "invoice:inv-1004"),
        ("commitment:com-205", "invoice:inv-1005"),
    }


def test_superseded_and_out_of_window_commitments(real_store: FalkorDBStore, varsha: str) -> None:
    r = _investigate(real_store, varsha)
    due = {c.ref for c in r.commitments}
    assert due == {"COM-201", "COM-202R", "COM-203", "COM-204"}  # COM-202 superseded,
    # COM-200 fulfilled, COM-205 due 5 Nov
    superseded = [f for f in r.findings if f.category == "superseded_commitment"]
    assert len(superseded) == 1 and "15 Oct 2026 -> 19 Oct 2026" in superseded[0].statement
    assert superseded[0].source_ids == ["source:src-009"]
    later = next(f for f in r.findings if "COM-205" in f.statement)
    assert "after this window" in later.statement
    wider = _investigate(real_store, varsha, horizon_days=31)
    assert "COM-205" in {c.ref for c in wider.commitments}


def test_cancelled_replacement_restores_the_earlier_version(
    real_store: FalkorDBStore, varsha: str
) -> None:
    report = CsvImporter(real_store, varsha).run(
        {
            "commitments.csv": "commitment_id,type,supplier_id,amount,due_date,status,supersedes,"
            "updated_at\nCOM-202R,supplier_payment,SUP-002,240000,2026-10-19,cancelled,COM-202,"
            "2026-10-16T12:00:00Z\n"
        }
    )
    assert report.written, report.errors
    due = {c.ref: c for c in _investigate(real_store, varsha).commitments}
    assert "COM-202R" not in due
    assert due["COM-202"].past_due  # original 15 Oct obligation is open again


def test_conflicts_missing_information_and_drafts(real_store: FalkorDBStore, varsha: str) -> None:
    r = _investigate(real_store, varsha)
    assert any("SRC-008" in w and "INV-1002" in w for w in r.warnings)
    assert any("INV-1004 has no due date" in m for m in r.missing_information)
    assert any("No source document supports commitment COM-203" in m for m in r.missing_information)
    drafts = {d.invoice_ref: d for d in r.follow_up_drafts}
    assert set(drafts) == {"INV-1001", "INV-1002", "INV-1003"}  # overdue only
    assert all(d.sent is False for d in r.follow_up_drafts)
    assert any("SRC-008" in n for n in drafts["INV-1002"].review_notes)
    assert "20 Oct 2026" in drafts["INV-1001"].message
    assert r.next_steps[0].startswith("Follow up with Shree Ganesh Contractors about INV-1001")
    assert r.queries_executed[:3] == ["cf_receivables", "cf_commitments_due", "cf_dependency_paths"]


def test_withdrawn_source_downgrades_the_dependency(real_store: FalkorDBStore, varsha: str) -> None:
    Ingestor(real_store, varsha).apply_event(
        SourceWithdrawn(
            type="source_withdrawn",
            event_id="evt-test-1",
            source_id="source:src-007",
            reason="message was about a different invoice",
            occurred_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
        )
    )
    r = _investigate(real_store, varsha)
    com201 = next(d for d in r.dependencies if d.commitment_id == "commitment:com-201")
    assert com201.certainty == "recorded_without_source"
    assert any("SRC-007" in w and "withdrawn" in w for w in r.warnings)


# ---------------------------------------------------------------- updates and history


def test_new_records_change_the_answer_and_stale_exports_do_not(
    real_store: FalkorDBStore, varsha: str
) -> None:
    as_of = date(2026, 10, 20)
    report = seed_cashflow(
        real_store, varsha, dataset="varsha-updates", observed_at=CASHFLOW_UPDATES_OBSERVED_AT
    )
    assert report.written
    after = _investigate(real_store, varsha, as_of=as_of)
    assert "INV-1001" not in {x.ref for x in after.receivables}
    assert any(
        "COM-201" in f.statement and "no outstanding balance" in f.statement for f in after.findings
    )
    inv1004 = next(x for x in after.receivables if x.ref == "INV-1004")
    assert inv1004.due_date == "2026-10-25"

    stale = seed_cashflow(real_store, varsha)  # the older base export again
    assert sorted(stale.ignored_stale) == ["invoice:inv-1001", "invoice:inv-1004"]
    again = _investigate(real_store, varsha, as_of=as_of)
    assert [f.statement for f in again.findings] == [f.statement for f in after.findings]


def test_payment_only_import_can_reference_existing_records(
    real_store: FalkorDBStore, varsha: str
) -> None:
    report = CsvImporter(real_store, varsha).run(
        {
            "payments.csv": "payment_id,customer_id,amount,status,received_on\n"
            "PAY-600,CUST-002,118000,cleared,2026-10-16\n",
            "allocations.csv": "payment_id,invoice_id,amount\nPAY-600,INV-1002,118000\n",
        },
        observed_at=datetime(2026, 10, 16, 15, 0, tzinfo=UTC),
    )
    assert report.valid and report.written, report.errors
    assert "INV-1002" not in {x.ref for x in _investigate(real_store, varsha).receivables}


def test_invalid_import_writes_nothing(
    real_store: FalkorDBStore, varsha: str, raw_graph: Any
) -> None:
    before = _snapshot(raw_graph, varsha)
    files = load_csv_dataset()
    files["payments.csv"] += "PAY-700,CUST-001,abc,cleared,,,,\n"
    report = CsvImporter(real_store, varsha).run(files)
    assert not report.valid and not report.written
    assert _snapshot(raw_graph, varsha) == before


# ---------------------------------------------------------------- isolation


def test_companies_are_isolated(
    real_store: FalkorDBStore, varsha: str, other_company: str, raw_graph: Any
) -> None:
    with pytest.raises(CompanyNotFound):
        _investigate(real_store, other_company)
    # a file marked for another company is refused before anything is written
    marked = "customer_id,name,company_id\nCUST-001,Someone Else," + varsha + "\n"
    refused = CsvImporter(real_store, other_company).run({"customers.csv": marked})
    assert not refused.valid
    assert not real_store.company_exists(other_company)
    # the other company's own records do not see or touch this company's graph
    before = _snapshot(raw_graph, varsha)
    ok = CsvImporter(real_store, other_company).run(
        {
            "customers.csv": "customer_id,name\nCUST-001,Other Co Customer\n",
            "invoices.csv": "invoice_id,customer_id,amount,due_date\n"
            "INV-1001,CUST-001,10,2026-01-01\n",
        }
    )
    assert ok.written
    assert _snapshot(raw_graph, varsha) == before
    other = _investigate(real_store, other_company)
    assert [x.customer_name for x in other.receivables] == ["Other Co Customer"]
    assert other.dependencies == []
    # references to the first company's records do not resolve in the second
    cross = CsvImporter(real_store, other_company).run(
        {"allocations.csv": "payment_id,invoice_id,amount\nPAY-501,INV-1002,1\n"}
    )
    assert not cross.valid


def test_http_import_and_investigation(
    settings: Settings, real_store: FalkorDBStore, company: str, other_company: str
) -> None:
    key = "test-key-cashflow-0123456"
    app = create_app(
        settings.model_copy(update={"company_brain_api_keys": SecretStr(f"{key}={company}")})
    )
    client = TestClient(app)
    client.headers["X-API-Key"] = key
    files = load_csv_dataset()
    dry = client.post(
        f"/v1/companies/{company}/cashflow/imports", json={"files": files, "dry_run": True}
    )
    assert dry.status_code == 200 and dry.json()["written"] is False
    assert not real_store.company_exists(company)
    response = client.post(
        f"/v1/companies/{company}/cashflow/imports",
        json={"files": files, "observed_at": "2026-10-15T18:00:00Z"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["entities"] == 43
    result = client.post(
        f"/v1/companies/{company}/cashflow/investigations",
        json={"as_of": "2026-10-16", "horizon_days": 7},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["summary"]["documented_dependencies"] == 2
    edge = body["evidence_paths"][0]["edges"][0]
    assert {"type", "from", "to", "source_id"} <= set(edge)
    denied = client.post(f"/v1/companies/{other_company}/cashflow/investigations", json={})
    assert denied.status_code == 403
    # the generic lookup endpoints work on the new kinds too
    entity = client.get(f"/v1/companies/{company}/entities/commitment:com-201")
    assert entity.status_code == 200
    assert {r["type"] for r in entity.json()["relationships"]} >= {"DEPENDS_ON", "OWED_TO"}
    paths = client.get(
        f"/v1/companies/{company}/connections",
        params={"from": "supplier:sup-001", "to": "customer:cust-001", "max_depth": 3},
    )
    assert paths.status_code == 200 and paths.json()
