"""Import Zoho Books records (read through Composio) into a company graph.

Mapping onto the Cashflow Radar types, so the receivables and payables investigation runs
on real books:

- customer and vendor contacts -> customer, supplier
- invoices -> invoice BILLED_TO customer (total as amount_paise)
- customer payments -> payment RECEIVED_FROM customer, ALLOCATED_TO each invoice it
  was applied to, with the applied amount
- bills -> commitment (supplier_payment) OWED_TO supplier, for the balance still due
- vendor payments -> payment PAID_TO supplier

Every record gets a source pointing at it in Zoho Books. Drafts are not issued, so they
are left out and counted; void invoices and bills are kept as cancelled. Anything that
cannot be mapped safely becomes a warning, never a guess. Amounts are integer paise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from company_brain.graph import GraphStore
from company_brain.ingest import Ingestor
from company_brain.models import EntityInput, RelationshipInput, SourceInput
from company_brain.schema import CURRENCIES, Rel

MAX_RECORDS = 5000

INVOICE_STATUS = {
    "sent": "open",
    "viewed": "open",
    "overdue": "open",
    "unpaid": "open",
    "partially_paid": "partially_paid",
    "paid": "paid",
    "void": "cancelled",
}
BILL_OPEN = {"open", "overdue", "partially_paid", "unpaid"}

Rows = list[dict[str, Any]]


class BooksImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str = Field(max_length=50)
    data_centre: str = Field(max_length=10, examples=["in"])
    contacts: Rows = Field(default_factory=list, max_length=MAX_RECORDS)
    invoices: Rows = Field(default_factory=list, max_length=MAX_RECORDS)
    bills: Rows = Field(default_factory=list, max_length=MAX_RECORDS)
    customer_payments: Rows = Field(
        default_factory=list,
        max_length=MAX_RECORDS,
        description="Payments with an 'invoices' list of {invoice_id, amount_applied}.",
    )
    vendor_payments: Rows = Field(default_factory=list, max_length=MAX_RECORDS)
    observed_at: datetime | None = None
    dry_run: bool = False


@dataclass
class BooksReport:
    customers: int = 0
    suppliers: int = 0
    invoices: int = 0
    commitments: int = 0
    payments: int = 0
    allocations: int = 0
    skipped_drafts: int = 0
    warnings: list[str] = field(default_factory=list)
    written: bool = False


def paise(value: Any) -> int | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _day(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat() if value else None
    except ValueError:
        return None


def _slug(value: Any) -> str:
    return "zb-" + "".join(ch for ch in str(value).lower() if ch.isalnum())[:60]


def import_books(store: GraphStore, company_id: str, req: BooksImport) -> BooksReport:
    report = BooksReport()
    observed = req.observed_at or datetime.now(UTC)
    app = f"https://books.zoho.{req.data_centre}/app/{req.organization_id}#"
    entities: dict[str, EntityInput] = {}
    sources: list[SourceInput] = []
    rels: list[RelationshipInput] = []

    def source(kind: str, rid: str, number: str, path: str, excerpt: str, supports: str) -> str:
        sid = f"source:{_slug(kind)}-{_slug(rid)[3:]}"
        sources.append(
            SourceInput(
                id=sid,
                name=f"Zoho Books {kind} {number}",
                source_type="accounting_record",
                uri=f"{app}/{path}/{rid}",
                excerpt=excerpt[:4000],
                source_system="zoho_books",
                source_record_id=number,
                supports=[supports],
                observed_at=observed,
            )
        )
        return sid

    def currency(row: dict[str, Any], what: str) -> str | None:
        code = row.get("currency_code")
        if code and code not in CURRENCIES:
            report.warnings.append(f"{what}: currency {code} is not supported; amount left out")
            return None
        return str(code) if code else None

    contact_kind: dict[str, str] = {}
    for c in req.contacts:
        cid = c.get("contact_id")
        kind = {"customer": "customer", "vendor": "supplier"}.get(str(c.get("contact_type")))
        if not cid or not kind:
            continue
        eid = f"{kind}:{_slug(cid)}"
        contact_kind[str(cid)] = eid
        entities[eid] = EntityInput(
            id=eid,
            name=str(c.get("contact_name") or c.get("company_name") or cid)[:300],
            properties={"source_system": "zoho_books", "source_record_id": str(cid)},
            observed_at=observed,
        )

    def party(row: dict[str, Any], id_key: str, name_key: str, kind: str) -> str | None:
        pid = row.get(id_key)
        if not pid:
            return None
        eid = contact_kind.get(str(pid)) or f"{kind}:{_slug(pid)}"
        entities.setdefault(
            eid,
            EntityInput(
                id=eid,
                name=str(row.get(name_key) or pid)[:300],
                properties={"source_system": "zoho_books", "source_record_id": str(pid)},
                observed_at=observed,
            ),
        )
        return eid

    invoice_ids: dict[str, str] = {}
    for inv in req.invoices:
        number = str(inv.get("invoice_number") or inv.get("invoice_id"))
        status = str(inv.get("status") or "")
        if status == "draft":
            report.skipped_drafts += 1
            continue
        mapped = INVOICE_STATUS.get(status)
        if mapped is None:
            report.warnings.append(
                f"invoice {number}: status {status!r} not recognised; left unset"
            )
        eid = f"invoice:{_slug(inv.get('invoice_id'))}"
        props: dict[str, Any] = {"source_system": "zoho_books", "source_record_id": number}
        if (code := currency(inv, f"invoice {number}")) is not None or not inv.get("currency_code"):
            if (amount := paise(inv.get("total"))) is not None:
                props["amount_paise"] = amount
            if code:
                props["currency"] = code
        if due := _day(inv.get("due_date")):
            props["due_date"] = due
        else:
            report.warnings.append(f"invoice {number}: no due date recorded")
        if issued := _day(inv.get("date")):
            props["issue_date"] = issued
        if mapped:
            props["status"] = mapped
        entities[eid] = EntityInput(
            id=eid, name=f"Invoice {number}", properties=props, observed_at=observed
        )
        invoice_ids[str(inv.get("invoice_id"))] = eid
        excerpt = (
            f"Invoice {number} to {inv.get('customer_name')}, total {inv.get('total')}, "
            f"balance {inv.get('balance')}, due {inv.get('due_date')}, status {status}"
        )
        sid = source("invoice", str(inv.get("invoice_id")), number, "invoices", excerpt, eid)
        if (cust := party(inv, "customer_id", "customer_name", "customer")) is not None:
            rels.append(
                RelationshipInput(
                    type=Rel.BILLED_TO, from_id=eid, to_id=cust, source_id=sid, observed_at=observed
                )
            )
        else:
            report.warnings.append(f"invoice {number}: no customer recorded")

    for bill in req.bills:
        number = str(bill.get("bill_number") or bill.get("bill_id"))
        status = str(bill.get("status") or "")
        if status == "draft":
            report.skipped_drafts += 1
            continue
        eid = f"commitment:{_slug(bill.get('bill_id'))}"
        balance = paise(bill.get("balance"))
        props = {
            "source_system": "zoho_books",
            "source_record_id": number,
            "commitment_type": "supplier_payment",
            "status": "cancelled"
            if status == "void"
            else "open"
            if status in BILL_OPEN and (balance or 0) > 0
            else "fulfilled",
        }
        if currency(bill, f"bill {number}") is not None or not bill.get("currency_code"):
            if balance is not None:
                props["amount_paise"] = balance
            if bill.get("currency_code"):
                props["currency"] = str(bill["currency_code"])
        if due := _day(bill.get("due_date")):
            props["due_date"] = due
        else:
            report.warnings.append(f"bill {number}: no due date recorded")
        entities[eid] = EntityInput(
            id=eid,
            name=f"Bill {number} from {bill.get('vendor_name')}"[:300],
            properties=props,
            observed_at=observed,
        )
        excerpt = (
            f"Bill {number} from {bill.get('vendor_name')}, total {bill.get('total')}, "
            f"balance {bill.get('balance')}, due {bill.get('due_date')}, status {status}"
        )
        sid = source("bill", str(bill.get("bill_id")), number, "bills", excerpt, eid)
        if (vendor := party(bill, "vendor_id", "vendor_name", "supplier")) is not None:
            rels.append(
                RelationshipInput(
                    type=Rel.OWED_TO, from_id=eid, to_id=vendor, source_id=sid, observed_at=observed
                )
            )

    for pay in req.customer_payments:
        number = str(pay.get("payment_number") or pay.get("payment_id"))
        eid = f"payment:{_slug(pay.get('payment_id'))}"
        props = {
            "source_system": "zoho_books",
            "source_record_id": number,
            "status": "cleared",  # Zoho records a customer payment once it is received
        }
        if (amount := paise(pay.get("amount"))) is not None:
            props["amount_paise"] = amount
        if received := _day(pay.get("date")):
            props["received_on"] = received
        if pay.get("payment_mode"):
            props["method"] = str(pay["payment_mode"])[:50]
        entities[eid] = EntityInput(
            id=eid, name=f"Payment {number}", properties=props, observed_at=observed
        )
        excerpt = f"Payment {number} from {pay.get('customer_name')}, amount {pay.get('amount')}"
        sid = source(
            "payment", str(pay.get("payment_id")), number, "paymentsreceived", excerpt, eid
        )
        if (cust := party(pay, "customer_id", "customer_name", "customer")) is not None:
            rels.append(
                RelationshipInput(
                    type=Rel.RECEIVED_FROM,
                    from_id=eid,
                    to_id=cust,
                    source_id=sid,
                    observed_at=observed,
                )
            )
        for applied in pay.get("invoices") or []:
            target = invoice_ids.get(str(applied.get("invoice_id")))
            amount = paise(applied.get("amount_applied"))
            if target is None:
                report.warnings.append(
                    f"payment {number}: applied to invoice {applied.get('invoice_number')}, "
                    "which was not imported; allocation left out"
                )
                continue
            rels.append(
                RelationshipInput(
                    type=Rel.ALLOCATED_TO,
                    from_id=eid,
                    to_id=target,
                    source_id=sid,
                    properties={"amount_paise": amount} if amount is not None else {},
                    observed_at=observed,
                )
            )
            report.allocations += 1

    for pay in req.vendor_payments:
        number = str(pay.get("payment_number") or pay.get("payment_id"))
        eid = f"payment:{_slug('v' + str(pay.get('payment_id')))}"
        props = {"source_system": "zoho_books", "source_record_id": number, "status": "cleared"}
        if (amount := paise(pay.get("amount"))) is not None:
            props["amount_paise"] = amount
        if paid := _day(pay.get("date")):
            props["paid_on"] = paid
        entities[eid] = EntityInput(
            id=eid,
            name=f"Payment {number} to {pay.get('vendor_name')}"[:300],
            properties=props,
            observed_at=observed,
        )
        excerpt = f"Payment {number} to {pay.get('vendor_name')}, amount {pay.get('amount')}"
        sid = source(
            "vendor payment", str(pay.get("payment_id")), number, "paymentsmade", excerpt, eid
        )
        if (vendor := party(pay, "vendor_id", "vendor_name", "supplier")) is not None:
            rels.append(
                RelationshipInput(
                    type=Rel.PAID_TO, from_id=eid, to_id=vendor, source_id=sid, observed_at=observed
                )
            )

    report.customers = sum(1 for e in entities if e.startswith("customer:"))
    report.suppliers = sum(1 for e in entities if e.startswith("supplier:"))
    report.invoices = sum(1 for e in entities if e.startswith("invoice:"))
    report.commitments = sum(1 for e in entities if e.startswith("commitment:"))
    report.payments = sum(1 for e in entities if e.startswith("payment:"))
    if req.dry_run:
        return report

    ingest = Ingestor(store, company_id)
    for entity in entities.values():
        ingest.upsert_entity(entity)
    for src in sources:
        ingest.register_source(src)
    for rel in rels:
        ingest.upsert_relationship(rel)
    report.written = True
    return report
