"""Cashflow Radar without a database: schema rules, validation, CSV import checks, API."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from company_brain import queries as q
from company_brain.api import create_app
from company_brain.cashflow import CashflowRequest, inr
from company_brain.config import Settings
from company_brain.csv_import import CsvImporter, record_id
from company_brain.graph import GraphUnavailable, Query, Row
from company_brain.models import EntityInput, RelationshipInput
from company_brain.schema import Kind, Rel, check_relationship
from company_brain.seed import load_csv_dataset
from tests.conftest import FakeStore

KEY = "key-varsha-0123456789ab"


def _writes(store: FakeStore) -> list[str]:
    writes = {query.name for query in q.all_queries() if not query.read_only}
    return [name for _, name, _ in store.calls if name in writes]


# ---------------------------------------------------------------- schema and models


def test_new_kinds_have_readable_labels() -> None:
    assert Kind.SALES_ORDER.label == "SalesOrder"
    assert Kind.PURCHASE_ORDER.label == "PurchaseOrder"
    assert Kind.INVOICE.label == "Invoice"
    assert record_id(Kind.INVOICE, "INV-1001") == "invoice:inv-1001"


def test_dependency_and_supersession_rules() -> None:
    check_relationship(Rel.DEPENDS_ON, Kind.COMMITMENT, Kind.INVOICE)
    check_relationship(Rel.SUPERSEDES, Kind.COMMITMENT, Kind.COMMITMENT)
    check_relationship(Rel.CONTRADICTS, Kind.SOURCE, Kind.INVOICE)
    with pytest.raises(ValueError, match="not allowed"):
        check_relationship(Rel.DEPENDS_ON, Kind.INVOICE, Kind.COMMITMENT)
    with pytest.raises(ValueError, match="same kind"):
        check_relationship(Rel.SUPERSEDES, Kind.COMMITMENT, Kind.DECISION)
    with pytest.raises(ValidationError, match="same kind"):
        RelationshipInput(type=Rel.SUPERSEDES, from_id="decision:a", to_id="commitment:b")


@pytest.mark.parametrize(
    ("properties", "match"),
    [
        ({"amount_paise": -1}, "amount_paise"),
        ({"amount_paise": 10.5}, "amount_paise"),
        ({"amount_paise": True}, "amount_paise"),
        ({"currency": "USD"}, "currency"),
        ({"due_date": "16/10/2026"}, "isoformat"),
        ({"issue_date": "yesterday"}, "isoformat"),
        ({"status": "settled"}, "status"),
        ({"gstin": "27ABCDE1234F1Z5"}, "not allowed"),
    ],
)
def test_invoice_properties_are_validated(properties: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        EntityInput(id="invoice:inv-1", name="Invoice", properties=properties)


def test_commitment_type_is_validated() -> None:
    EntityInput(id="commitment:c", name="C", properties={"commitment_type": "supplier_payment"})
    with pytest.raises(ValidationError, match="commitment_type"):
        EntityInput(id="commitment:c", name="C", properties={"commitment_type": "loan"})


@pytest.mark.parametrize(
    ("paise", "text"),
    [
        (0, "₹0"),
        (5000, "₹50"),
        (30_000_000, "₹3,00,000"),
        (8_500_050, "₹85,000.50"),
        (1_234_567_890_1, "₹12,34,56,789.01"),
        (None, "an unrecorded amount"),
    ],
)
def test_inr_uses_indian_grouping(paise: int | None, text: str) -> None:
    assert inr(paise) == text


def test_cashflow_request_bounds() -> None:
    assert CashflowRequest().horizon_days == 7
    with pytest.raises(ValidationError):
        CashflowRequest(horizon_days=0)
    with pytest.raises(ValidationError):
        CashflowRequest(horizon_days=90)
    with pytest.raises(ValidationError):
        CashflowRequest(cypher="MATCH (n) RETURN n")  # type: ignore[call-arg]


def test_cashflow_queries_are_parameterized_and_bounded() -> None:
    for query in (q.CF_RECEIVABLES, q.CF_COMMITMENTS_DUE):
        assert "LIMIT $limit" in query.cypher
        assert query.read_only
    # dependencies are only ever followed through recorded DEPENDS_ON edges
    assert "DEPENDS_ON" in q.CF_DEPENDENCY_PATHS.cypher
    assert "superseders = 0" in q.CF_COMMITMENTS_DUE.cypher


# ---------------------------------------------------------------- CSV validation


def test_packaged_dataset_is_valid_and_dry_run_writes_nothing() -> None:
    store = FakeStore()
    report = CsvImporter(store, "varsha-electricals").run(load_csv_dataset(), dry_run=True)
    assert report.valid, report.errors
    assert not report.written
    assert report.entities == 43 and report.relationships == 64
    assert _writes(store) == []
    # the one dependency without a source document is flagged, not rejected
    assert [w.row for w in report.warnings if w.file == "dependencies.csv"] == [2]


def _import(files: dict[str, str], **kwargs: Any) -> tuple[Any, FakeStore]:
    store = FakeStore()
    return CsvImporter(store, "varsha").run(files, **kwargs), store


CUSTOMERS = "customer_id,name\nCUST-001,Shree Ganesh\n"
INVOICE_HEADER = "invoice_id,customer_id,issue_date,due_date,amount,status\n"


@pytest.mark.parametrize(
    ("files", "file", "row", "column", "message"),
    [
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,,16/10/2026,100,open\n",
            },
            "invoices.csv",
            1,
            "due_date",
            "YYYY-MM-DD",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,,,12.345,open\n",
            },
            "invoices.csv",
            1,
            "amount",
            "two decimal",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,,,-5,open\n",
            },
            "invoices.csv",
            1,
            "amount",
            "positive",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,,,lots,open\n",
            },
            "invoices.csv",
            1,
            "amount",
            "not an amount",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,,,100,settled\n",
            },
            "invoices.csv",
            1,
            "status",
            "not valid",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,2026-10-02,2026-10-01,100,open\n",
            },
            "invoices.csv",
            1,
            "due_date",
            "before issue date",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV-1,CUST-404,,,100,open\n",
            },
            "invoices.csv",
            1,
            "customer_id",
            "customer CUST-404 is not in this import",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": INVOICE_HEADER + "INV 1,CUST-001,,,100,open\n",
            },
            "invoices.csv",
            1,
            "invoice_id",
            "not a valid id",
        ),
        (
            {"customers.csv": CUSTOMERS + "cust-001,Duplicate\n"},
            "customers.csv",
            2,
            "customer_id",
            "defined twice",
        ),
        (
            {"customers.csv": "customer_id,name,company_id\nCUST-001,Shree Ganesh,acme\n"},
            "customers.csv",
            1,
            "company_id",
            "belongs to company 'acme'",
        ),
        (
            {"customers.csv": "customer_id,name,phone\nCUST-001,Shree Ganesh,98\n"},
            "customers.csv",
            0,
            "phone",
            "unknown column",
        ),
        (
            {"customers.csv": "customer_id\nCUST-001\n"},
            "customers.csv",
            0,
            "name",
            "required column missing",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "invoices.csv": "invoice_id,customer_id,amount,currency\nINV-1,CUST-001,100,USD\n",
            },
            "invoices.csv",
            1,
            "currency",
            "INR",
        ),
        (
            {
                "customers.csv": CUSTOMERS,
                "commitments.csv": "commitment_id,type,customer_id\n"
                "COM-1,supplier_payment,CUST-001\n",
            },
            "commitments.csv",
            1,
            "customer_id",
            "owed to a supplier",
        ),
        (
            {"commitments.csv": "commitment_id,type,supersedes\nCOM-1,supplier_payment,COM-1\n"},
            "commitments.csv",
            1,
            "supersedes",
            "itself",
        ),
        (
            {"sources.csv": "source_id,title,source_type,contradicts\nS-1,Mail,email,INV-1\n"},
            "sources.csv",
            1,
            "contradicts",
            "<kind>:<id>",
        ),
        (
            {"sources.csv": "source_id,title,source_type\nS-1,Mail,fax\n"},
            "sources.csv",
            1,
            "source_type",
            "must be one of",
        ),
    ],
)
def test_row_level_errors_stop_the_import(
    files: dict[str, str], file: str, row: int, column: str, message: str
) -> None:
    report, store = _import(files)
    assert not report.valid and not report.written
    assert any(
        e.file == file and e.row == row and e.column == column and message in e.message
        for e in report.errors
    ), report.errors
    assert _writes(store) == []


def test_unknown_file_and_extra_cells_are_reported() -> None:
    report, store = _import(
        {"ledger.csv": "a\n1\n", "customers.csv": "customer_id,name\nCUST-1,A,extra\n"}
    )
    messages = {(e.file, e.message.split(";")[0]) for e in report.errors}
    assert ("ledger.csv", "unknown file") in messages
    assert ("customers.csv", "row has extra cells") in messages
    assert _writes(store) == []


def test_allocations_cannot_exceed_the_payment() -> None:
    files = {
        "customers.csv": CUSTOMERS,
        "invoices.csv": "invoice_id,customer_id,amount\nINV-1,CUST-001,500\nINV-2,CUST-001,500\n",
        "payments.csv": "payment_id,customer_id,amount,status\nPAY-1,CUST-001,600,cleared\n",
        "allocations.csv": "payment_id,invoice_id,amount\nPAY-1,INV-1,400\nPAY-1,INV-2,400\n",
    }
    report, store = _import(files)
    assert [e.message for e in report.errors if e.file == "allocations.csv"] == [
        "allocations for PAY-1 total 800.00, more than the payment amount 600.00 "
        "(payments.csv row 1)"
    ]
    assert _writes(store) == []


def test_duplicate_allocation_rows_are_rejected() -> None:
    files = {
        "customers.csv": CUSTOMERS,
        "invoices.csv": "invoice_id,customer_id,amount\nINV-1,CUST-001,500\n",
        "payments.csv": "payment_id,customer_id,amount,status\nPAY-1,CUST-001,600,cleared\n",
        "allocations.csv": "payment_id,invoice_id,amount\nPAY-1,INV-1,100\nPAY-1,INV-1,100\n",
    }
    report, _ = _import(files)
    assert any("already allocated" in e.message for e in report.errors)


def test_amounts_accept_indian_formatting_and_store_paise() -> None:
    store = FakeStore()
    report = CsvImporter(store, "varsha").run(
        {
            "customers.csv": CUSTOMERS,
            "invoices.csv": 'invoice_id,customer_id,amount\nINV-1,CUST-001,"₹4,72,000.50"\n',
        },
        dry_run=True,
    )
    assert report.valid, report.errors


def test_references_may_point_at_records_already_in_the_graph() -> None:
    store = FakeStore(
        {
            "entities_exist": [
                {"id": "customer:cust-001", "kind": "customer"},
                {"id": "invoice:inv-1", "kind": "invoice"},
            ]
        }
    )
    store.companies.add("varsha")
    report = CsvImporter(store, "varsha").run(
        {
            "payments.csv": "payment_id,customer_id,amount,status\nPAY-9,CUST-001,100,cleared\n",
            "allocations.csv": "payment_id,invoice_id,amount\nPAY-9,INV-1,100\n",
        },
        dry_run=True,
    )
    assert report.valid, report.errors
    assert ("varsha", "entities_exist") in {(c, name) for c, name, _ in store.calls}
    # every graph call went to the importing company only
    assert {company for company, _, _ in store.calls} == {"varsha"}


# ---------------------------------------------------------------- API


def _client(store: FakeStore, keys: str = f"{KEY}=varsha") -> TestClient:
    settings = Settings(company_brain_api_keys=SecretStr(keys))
    client = TestClient(create_app(settings, store_factory=lambda _: store))
    client.headers["X-API-Key"] = KEY
    return client


def test_import_endpoint_is_company_scoped() -> None:
    store = FakeStore()
    body = {"files": {"customers.csv": CUSTOMERS}}
    assert _client(store).post("/v1/companies/acme/cashflow/imports", json=body).status_code == 403
    assert store.calls == []
    ok = _client(store).post("/v1/companies/varsha/cashflow/imports", json=body)
    assert ok.status_code == 200 and ok.json()["written"] is True
    assert {c[0] for c in store.calls} == {"varsha"}


def test_import_endpoint_returns_422_with_row_errors_and_writes_nothing() -> None:
    store = FakeStore()
    response = _client(store).post(
        "/v1/companies/varsha/cashflow/imports",
        json={"files": {"customers.csv": "customer_id,name\nCUST 1,A\n"}},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["valid"] is False and body["written"] is False
    assert body["errors"][0]["row"] == 1 and body["errors"][0]["column"] == "customer_id"
    assert _writes(store) == []


def test_import_endpoint_rejects_unknown_fields() -> None:
    response = _client(FakeStore()).post(
        "/v1/companies/varsha/cashflow/imports",
        json={"files": {"customers.csv": CUSTOMERS}, "company_id": "acme"},
    )
    assert response.status_code == 422


def test_investigation_on_unknown_company_is_404() -> None:
    response = _client(FakeStore()).post("/v1/companies/varsha/cashflow/investigations", json={})
    assert response.status_code == 404


class _DownStore(FakeStore):
    def run(self, company_id: str, query: Query, params: dict[str, Any] | None = None) -> list[Row]:
        raise GraphUnavailable("down")

    def company_exists(self, company_id: str) -> bool:
        raise GraphUnavailable("down")


def test_database_unavailable_returns_503_not_an_empty_answer() -> None:
    client = _client(_DownStore())
    investigation = client.post("/v1/companies/varsha/cashflow/investigations", json={})
    assert investigation.status_code == 503
    imported = client.post(
        "/v1/companies/varsha/cashflow/imports",
        json={"files": {"invoices.csv": INVOICE_HEADER + "INV-1,CUST-001,,,100,open\n"}},
    )
    assert imported.status_code == 503


def test_agent_tool_definitions_match_the_api() -> None:
    import json
    from pathlib import Path

    from company_brain.contract_cost import ContractCostRequest

    spec = json.loads(
        (Path(__file__).parent.parent / "examples/agent/contract_tools.json").read_text("utf-8")
    )
    tools = {t["name"]: t for t in spec["tools"]}
    request = ContractCostRequest.model_json_schema()
    assert set(tools["investigate_contract"]["input_schema"]["properties"]) == set(
        request["properties"]
    )
    assert tools["investigate_contract"]["input_schema"]["required"] == request["required"]
    paths = _client(FakeStore()).get("/openapi.json").json()["paths"]
    for tool in tools.values():
        endpoint = tool["endpoint"]
        assert endpoint["path"].replace("{company}", "{company_id}") in paths, tool["name"]
