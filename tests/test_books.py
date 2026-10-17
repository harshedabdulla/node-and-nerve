"""Zoho Books import: shapes as Zoho returns them, mapped onto the cashflow types."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from company_brain.books import BooksImport, import_books, paise
from company_brain.cashflow import CashflowInvestigator, CashflowRequest
from company_brain.graph import FalkorDBStore
from tests.conftest import FakeStore

CONTACTS = [
    {"contact_id": "100", "contact_name": "NN test Salem Traders", "contact_type": "customer"},
    {"contact_id": "200", "contact_name": "NN test Steel Supplies", "contact_type": "vendor"},
]
INVOICES = [
    {
        "invoice_id": "1",
        "invoice_number": "INV-000001",
        "customer_id": "100",
        "customer_name": "NN test Salem Traders",
        "date": "2026-09-01",
        "due_date": "2026-09-30",
        "total": 240000.0,
        "balance": 240000.0,
        "status": "overdue",
        "currency_code": "INR",
    },
    {
        "invoice_id": "2",
        "invoice_number": "INV-000002",
        "customer_id": "100",
        "customer_name": "NN test Salem Traders",
        "date": "2026-09-15",
        "due_date": "2026-10-15",
        "total": 160000.5,
        "balance": 100000.5,
        "status": "overdue",
        "currency_code": "INR",
    },
    {
        "invoice_id": "3",
        "invoice_number": "INV-000003",
        "customer_id": "100",
        "date": "2026-10-15",
        "due_date": "2026-11-15",
        "total": 45000,
        "balance": 45000,
        "status": "draft",
        "currency_code": "INR",
    },
]
BILLS = [
    {
        "bill_id": "9",
        "bill_number": "NN-TEST-B1",
        "vendor_id": "200",
        "vendor_name": "NN test Steel Supplies",
        "date": "2026-09-05",
        "due_date": "2026-10-20",
        "total": 180000,
        "balance": 180000,
        "status": "open",
        "currency_code": "INR",
    },
]
PAYMENTS = [
    {
        "payment_id": "50",
        "payment_number": "2",
        "customer_id": "100",
        "customer_name": "NN test Salem Traders",
        "amount": 60000,
        "date": "2026-10-12",
        "payment_mode": "banktransfer",
        "invoices": [
            {"invoice_id": "2", "invoice_number": "INV-000002", "amount_applied": 60000},
            {"invoice_id": "77", "invoice_number": "INV-OLD", "amount_applied": 10},
        ],
    },
]
VENDOR_PAYMENTS = [
    {
        "payment_id": "60",
        "payment_number": "1",
        "vendor_id": "200",
        "vendor_name": "NN test Steel Supplies",
        "amount": 15000,
        "date": "2026-10-10",
    },
]


def _req(**kw: Any) -> BooksImport:
    return BooksImport(
        organization_id="60012345",
        data_centre="in",
        contacts=CONTACTS,
        invoices=INVOICES,
        bills=BILLS,
        customer_payments=PAYMENTS,
        vendor_payments=VENDOR_PAYMENTS,
        observed_at=datetime(2026, 10, 18, tzinfo=UTC),
        **kw,
    )


def test_paise_is_exact() -> None:
    assert paise(160000.5) == 16000050
    assert paise("0.1") == 10 and paise(-5) is None and paise("x") is None


def test_drafts_are_left_out_and_unknown_allocations_reported() -> None:
    r = import_books(FakeStore(), "x", _req(dry_run=True))
    assert (r.customers, r.suppliers, r.invoices, r.commitments, r.payments) == (1, 1, 2, 1, 2)
    assert r.skipped_drafts == 1 and r.allocations == 1
    assert any("INV-OLD" in w and "not imported" in w for w in r.warnings)


def test_books_feed_the_cashflow_investigation(
    real_store: FalkorDBStore, company: str, raw_graph: Any
) -> None:
    import_books(real_store, company, _req())
    import_books(real_store, company, _req())  # re-import changes nothing
    g = raw_graph(company)
    assert g.ro_query("MATCH (i:Invoice) RETURN count(i)").result_set[0][0] == 2
    alloc = g.ro_query(
        "MATCH (:Payment)-[a:ALLOCATED_TO]->(i:Invoice {id: 'invoice:zb-2'}) RETURN a.amount_paise"
    ).result_set
    assert alloc == [[6000000]]
    uri = g.ro_query(
        "MATCH (:Invoice {id: 'invoice:zb-1'})-[:SUPPORTED_BY]->(s:Source) RETURN s.uri"
    ).result_set[0][0]
    assert uri == "https://books.zoho.in/app/60012345#/invoices/1"

    r = CashflowInvestigator(real_store, company).investigate(
        CashflowRequest(as_of=date(2026, 10, 18), horizon_days=7)
    )
    owed = {x.ref: x.outstanding_paise for x in r.receivables}
    assert owed == {"INV-000001": 24000000, "INV-000002": 10000050}  # 1,60,000.50 - 60,000
    assert [c.ref for c in r.commitments] == ["NN-TEST-B1"]


def test_money_tool_reports_rupees_from_the_engine(real_store: FalkorDBStore, company: str) -> None:
    from company_brain.workspace_agent import WorkspaceTools

    import_books(real_store, company, _req())
    result = WorkspaceTools(real_store, company, as_of=date(2026, 10, 18)).run(
        "money", {"days_ahead": 7}
    )
    assert result["owed_to_us_rupees"] == "₹3,40,000.50"  # 2,40,000 + 1,00,000.50
    overdue = {i["invoice"]: i["days_overdue"] for i in result["open_invoices"]}
    assert overdue == {"INV-000001": 18, "INV-000002": 3}
    assert result["bills_due"][0]["bill"] == "NN-TEST-B1"
