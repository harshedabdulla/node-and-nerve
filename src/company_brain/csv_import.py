"""CSV import for Cashflow Radar business records.

The import runs in two phases:

1. **Validate everything.** Every file and row is parsed, typed and checked, and every
   reference (a customer on an invoice, an invoice on an allocation, ...) must resolve
   to a record in this import or one already in the company's graph. Any error stops
   the import before anything is written, and the report lists each error by file,
   row and column.
2. **Write.** Entities first, then relationships, each as an idempotent MERGE keyed on
   a stable id derived from the business id (``INV-1001`` -> ``invoice:inv-1001``).
   Re-importing the same files changes nothing. A row with an ``updated_at`` older than
   what the graph holds is ignored, so out-of-order imports cannot overwrite newer data.

FalkorDB has no multi-query transactions, so phase 2 is not atomic. If the database
fails part-way, the report says so; re-running the same import completes it, because
every write is idempotent.

Company isolation: rows are written only to the graph of the company the import was
started for. A ``company_id`` column is optional; when present it must equal that
company on every row, which catches files exported for another tenant.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from company_brain import queries as q
from company_brain.graph import GraphError, GraphStore
from company_brain.ingest import Ingestor
from company_brain.models import EntityInput, RelationshipInput, utc_now
from company_brain.schema import COMMITMENT_TYPES, SOURCE_TYPES, STATUS_VALUES, Kind, Rel

MAX_FILE_CHARS = 2_000_000
MAX_ROWS_PER_FILE = 5_000
MAX_AMOUNT_PAISE = 10**15  # 10 lakh crore rupees: anything larger is a typo

_BUSINESS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# file -> (required columns, optional columns). Order is the write order.
FILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "sources.csv": (
        ("source_id", "title", "source_type"),
        ("uri", "excerpt", "recorded_at", "contradicts"),
    ),
    "customers.csv": (("customer_id", "name"), ("city",)),
    "suppliers.csv": (("supplier_id", "name"), ("city",)),
    "purchase_orders.csv": (
        ("purchase_order_id", "supplier_id"),
        ("order_date", "expected_delivery", "amount", "currency", "status"),
    ),
    "sales_orders.csv": (
        ("sales_order_id", "customer_id"),
        ("order_date", "required_by", "amount", "currency", "status", "purchase_order_id"),
    ),
    "invoices.csv": (
        ("invoice_id", "customer_id", "amount"),
        (
            "sales_order_id",
            "issue_date",
            "due_date",
            "currency",
            "status",
            "expected_payment_date",
        ),
    ),
    "payments.csv": (
        ("payment_id", "customer_id", "amount", "status"),
        ("received_on", "currency", "method", "reference"),
    ),
    "allocations.csv": (("payment_id", "invoice_id", "amount"), ()),
    "commitments.csv": (
        ("commitment_id", "type"),
        (
            "description",
            "supplier_id",
            "customer_id",
            "purchase_order_id",
            "sales_order_id",
            "amount",
            "currency",
            "due_date",
            "status",
            "supersedes",
        ),
    ),
    "dependencies.csv": (("commitment_id", "invoice_id"), ("note",)),
}
# Every file may carry these.
COMMON_COLUMNS = ("company_id", "source_ids", "updated_at")
# Records that may be named in sources.csv `contradicts` as "<kind>:<business id>".
_CONTRADICTABLE = {Kind.INVOICE, Kind.PAYMENT, Kind.COMMITMENT}


class ImportIssue(BaseModel):
    file: str
    row: int | None = Field(
        default=None, description="1-based data row (the header is row 0); null for file-level"
    )
    column: str | None = None
    message: str


class ImportReport(BaseModel):
    company_id: str
    dry_run: bool
    valid: bool = Field(description="True when every row passed validation.")
    written: bool = Field(description="True when all writes completed.")
    rows: dict[str, int] = Field(description="Data rows read per file.")
    entities: int = 0
    relationships: int = 0
    ignored_stale: list[str] = Field(
        default_factory=list,
        description="Records not updated because the graph already holds a newer version.",
    )
    errors: list[ImportIssue] = Field(default_factory=list)
    warnings: list[ImportIssue] = Field(default_factory=list)


def record_id(kind: Kind, business_id: str) -> str:
    """Stable graph id for a business id, e.g. ``(INVOICE, "INV-1001") -> invoice:inv-1001``."""
    return f"{kind.value}:{business_id.lower()}"


@dataclass
class _Ref:
    file: str
    row: int
    column: str
    target: str
    raw: str


@dataclass
class _Plan:
    entities: list[EntityInput] = field(default_factory=list)
    relationships: list[RelationshipInput] = field(default_factory=list)
    defined: dict[str, tuple[str, int]] = field(default_factory=dict)  # id -> (file, row)
    raw_ids: dict[str, str] = field(default_factory=dict)  # id -> business id as written
    refs: list[_Ref] = field(default_factory=list)


class _RowError(Exception):
    def __init__(self, column: str | None, message: str) -> None:
        super().__init__(message)
        self.column = column
        self.message = message


class CsvImporter:
    def __init__(self, store: GraphStore, company_id: str) -> None:
        self.store = store
        self.company_id = company_id

    def run(
        self,
        files: dict[str, str],
        *,
        dry_run: bool = False,
        observed_at: datetime | None = None,
        source_system: str = "csv",
    ) -> ImportReport:
        """Validate ``files`` (file name -> CSV text) and, unless invalid or dry-run, write."""
        report = ImportReport(
            company_id=self.company_id, dry_run=dry_run, valid=False, written=False, rows={}
        )
        default_ts = observed_at or utc_now()
        plan = _Plan()
        parsed = self._read_files(files, report)
        for name in FILES:
            for row_no, row in parsed.get(name, []):
                try:
                    self._parse_row(name, row_no, row, plan, default_ts, source_system, report)
                except _RowError as exc:
                    report.errors.append(
                        ImportIssue(file=name, row=row_no, column=exc.column, message=exc.message)
                    )
        self._check_references(plan, report)
        self._check_allocations(plan, report)
        report.valid = not report.errors
        report.entities = len(plan.entities)
        report.relationships = len(plan.relationships)
        if not report.valid or dry_run:
            return report
        self._write(plan, report)
        return report

    # ------------------------------------------------------------ reading

    def _read_files(
        self, files: dict[str, str], report: ImportReport
    ) -> dict[str, list[tuple[int, dict[str, str | None]]]]:
        parsed: dict[str, list[tuple[int, dict[str, str | None]]]] = {}
        if not files:
            report.errors.append(ImportIssue(file="(import)", message="no files were provided"))
        for name, text in sorted(files.items()):
            if name not in FILES:
                report.errors.append(
                    ImportIssue(
                        file=name,
                        message=f"unknown file; expected one of {', '.join(FILES)}",
                    )
                )
                continue
            if len(text) > MAX_FILE_CHARS:
                report.errors.append(
                    ImportIssue(file=name, message=f"file is larger than {MAX_FILE_CHARS} chars")
                )
                continue
            reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
            header = [h.strip() for h in (reader.fieldnames or [])]
            required, optional = FILES[name]
            missing = [c for c in required if c not in header]
            unknown = [c for c in header if c not in (*required, *optional, *COMMON_COLUMNS)]
            dupes = sorted({c for c in header if header.count(c) > 1})
            if missing or unknown or dupes:
                for c in missing:
                    report.errors.append(
                        ImportIssue(file=name, row=0, column=c, message="required column missing")
                    )
                for c in unknown:
                    report.errors.append(
                        ImportIssue(
                            file=name,
                            row=0,
                            column=c,
                            message="unknown column (check spelling against the template)",
                        )
                    )
                for c in dupes:
                    report.errors.append(
                        ImportIssue(file=name, row=0, column=c, message="duplicate column")
                    )
                continue
            reader.fieldnames = header
            rows: list[tuple[int, dict[str, str | None]]] = []
            for row_no, raw in enumerate(reader, start=1):
                if row_no > MAX_ROWS_PER_FILE:
                    report.errors.append(
                        ImportIssue(file=name, message=f"more than {MAX_ROWS_PER_FILE} rows")
                    )
                    break
                if None in raw:  # more cells than header columns
                    report.errors.append(
                        ImportIssue(file=name, row=row_no, message="row has extra cells")
                    )
                    continue
                cleaned = {
                    k: (v.strip() or None) if v is not None else None for k, v in raw.items()
                }
                if any(cleaned.values()):
                    rows.append((row_no, cleaned))
            report.rows[name] = len(rows)
            parsed[name] = rows
        return parsed

    # ------------------------------------------------------------ parsing

    def _parse_row(
        self,
        name: str,
        row_no: int,
        row: dict[str, str | None],
        plan: _Plan,
        default_ts: datetime,
        source_system: str,
        report: ImportReport,
    ) -> None:
        company = row.get("company_id")
        if company is not None and company != self.company_id:
            raise _RowError(
                "company_id",
                f"row belongs to company {company!r}, but this import is for "
                f"{self.company_id!r}; nothing was imported",
            )
        ts = _timestamp(row.get("updated_at"), default_ts)
        sources = [
            self._ref(plan, name, row_no, "source_ids", Kind.SOURCE, s)
            for s in _split(row.get("source_ids"))
        ]
        edge_source = sources[0] if sources else None
        system = f"{source_system}:{name}"

        def entity(
            kind: Kind, id_col: str, props: dict[str, Any], *, label: str, title: str | None = None
        ) -> str:
            raw = _required(row, id_col)
            eid = _business_id(kind, raw, id_col)
            if eid in plan.defined:
                first_file, first_row = plan.defined[eid]
                where = f"row {first_row}" + ("" if first_file == name else f" of {first_file}")
                raise _RowError(id_col, f"{raw} is defined twice (first at {where})")
            props = {k: v for k, v in props.items() if v is not None}
            props.update(source_system=system, source_record_id=raw)
            try:
                plan.entities.append(
                    EntityInput(
                        id=eid,
                        name=title or f"{label} {raw}",
                        properties=props,
                        observed_at=ts,
                    )
                )
            except ValidationError as exc:
                raise _RowError(None, _first_error(exc)) from None
            plan.defined[eid] = (name, row_no)
            plan.raw_ids[eid] = raw
            for sid in sources:
                link(Rel.SUPPORTED_BY, eid, sid, None)
            return eid

        def link(
            rel: Rel,
            from_id: str,
            to_id: str,
            source_id: str | None,
            props: dict[str, Any] | None = None,
        ) -> None:
            try:
                plan.relationships.append(
                    RelationshipInput(
                        type=rel,
                        from_id=from_id,
                        to_id=to_id,
                        source_id=source_id,
                        properties=props or {},
                        observed_at=ts,
                    )
                )
            except ValidationError as exc:
                raise _RowError(None, _first_error(exc)) from None

        def ref(column: str, kind: Kind, *, required: bool = False) -> str | None:
            raw = _required(row, column) if required else row.get(column)
            return self._ref(plan, name, row_no, column, kind, raw) if raw else None

        def status(kind: Kind, *, required: bool = False) -> str | None:
            value = _required(row, "status") if required else row.get("status")
            if value is not None and value not in STATUS_VALUES[kind]:
                allowed = ", ".join(sorted(STATUS_VALUES[kind]))
                raise _RowError("status", f"{value!r} is not valid; use one of {allowed}")
            return value

        if name == "sources.csv":
            if sources:
                raise _RowError("source_ids", "a source cannot cite other sources")
            stype = _required(row, "source_type")
            if stype not in SOURCE_TYPES:
                raise _RowError("source_type", f"must be one of {', '.join(sorted(SOURCE_TYPES))}")
            sid = entity(
                Kind.SOURCE,
                "source_id",
                label="Source",
                title=_required(row, "title"),
                props={
                    "source_type": stype,
                    "uri": row.get("uri"),
                    "excerpt": row.get("excerpt"),
                    "published_at": _date(row, "recorded_at"),
                },
            )
            for target in _split(row.get("contradicts")):
                kind_text, sep, raw = target.partition(":")
                kinds = {k.value: k for k in _CONTRADICTABLE}
                if not sep or kind_text not in kinds:
                    raise _RowError(
                        "contradicts",
                        f"{target!r}: write records as <kind>:<id>, kind one of "
                        f"{', '.join(sorted(kinds))} (e.g. invoice:INV-1002)",
                    )
                tid = self._ref(plan, name, row_no, "contradicts", kinds[kind_text], raw)
                link(Rel.CONTRADICTS, sid, tid, None)

        elif name == "customers.csv":
            entity(
                Kind.CUSTOMER,
                "customer_id",
                {"city": row.get("city")},
                label="Customer",
                title=_required(row, "name"),
            )

        elif name == "suppliers.csv":
            entity(
                Kind.SUPPLIER,
                "supplier_id",
                {"city": row.get("city")},
                label="Supplier",
                title=_required(row, "name"),
            )

        elif name == "purchase_orders.csv":
            supplier = ref("supplier_id", Kind.SUPPLIER, required=True)
            po = entity(
                Kind.PURCHASE_ORDER,
                "purchase_order_id",
                label="Purchase order",
                props={
                    "status": status(Kind.PURCHASE_ORDER),
                    "order_date": _date(row, "order_date"),
                    "expected_delivery": _date(row, "expected_delivery"),
                    **_money(row, "amount"),
                },
            )
            link(Rel.PLACED_WITH, po, _must(supplier), edge_source)

        elif name == "sales_orders.csv":
            customer = ref("customer_id", Kind.CUSTOMER, required=True)
            po_ref = ref("purchase_order_id", Kind.PURCHASE_ORDER)
            so = entity(
                Kind.SALES_ORDER,
                "sales_order_id",
                label="Sales order",
                props={
                    "status": status(Kind.SALES_ORDER),
                    "order_date": _date(row, "order_date"),
                    "required_by": _date(row, "required_by"),
                    **_money(row, "amount"),
                },
            )
            link(Rel.PLACED_BY, so, _must(customer), edge_source)
            if po_ref:
                link(Rel.SOURCED_FROM, so, po_ref, edge_source)

        elif name == "invoices.csv":
            customer = ref("customer_id", Kind.CUSTOMER, required=True)
            so_ref = ref("sales_order_id", Kind.SALES_ORDER)
            issue, due = _date(row, "issue_date"), _date(row, "due_date")
            if issue and due and due < issue:
                raise _RowError("due_date", f"due date {due} is before issue date {issue}")
            inv = entity(
                Kind.INVOICE,
                "invoice_id",
                label="Invoice",
                props={
                    "status": status(Kind.INVOICE),
                    "issue_date": issue,
                    "due_date": due,
                    "expected_payment_date": _date(row, "expected_payment_date"),
                    **_money(row, "amount", required=True),
                },
            )
            link(Rel.BILLED_TO, inv, _must(customer), edge_source)
            if so_ref:
                link(Rel.FOR_ORDER, inv, so_ref, edge_source)

        elif name == "payments.csv":
            customer = ref("customer_id", Kind.CUSTOMER, required=True)
            pay = entity(
                Kind.PAYMENT,
                "payment_id",
                label="Payment",
                props={
                    "status": status(Kind.PAYMENT, required=True),
                    "received_on": _date(row, "received_on"),
                    "method": row.get("method"),
                    "reference": row.get("reference"),
                    **_money(row, "amount", required=True),
                },
            )
            link(Rel.RECEIVED_FROM, pay, _must(customer), edge_source)

        elif name == "allocations.csv":
            pay_ref = _must(ref("payment_id", Kind.PAYMENT, required=True))
            inv_ref = _must(ref("invoice_id", Kind.INVOICE, required=True))
            amount = _money(row, "amount", required=True)["amount_paise"]
            if amount == 0:
                raise _RowError("amount", "allocation amount must be greater than zero")
            for existing in plan.relationships:
                if existing.type is Rel.ALLOCATED_TO and (existing.from_id, existing.to_id) == (
                    pay_ref,
                    inv_ref,
                ):
                    raise _RowError(
                        "invoice_id",
                        "this payment is already allocated to this invoice in an earlier row; "
                        "combine the amounts into one row",
                    )
            link(Rel.ALLOCATED_TO, pay_ref, inv_ref, edge_source, {"amount_paise": amount})
            for sid in sources:
                link(Rel.SUPPORTED_BY, pay_ref, sid, None)

        elif name == "commitments.csv":
            ctype = _required(row, "type")
            if ctype not in COMMITMENT_TYPES:
                raise _RowError("type", f"must be one of {', '.join(sorted(COMMITMENT_TYPES))}")
            supplier = ref("supplier_id", Kind.SUPPLIER)
            customer = ref("customer_id", Kind.CUSTOMER)
            if supplier and customer:
                raise _RowError(
                    "customer_id", "a commitment is owed to a supplier or a customer, not both"
                )
            if ctype == "supplier_payment" and customer:
                raise _RowError("customer_id", "a supplier_payment is owed to a supplier")
            orders = [
                o
                for o in (
                    ref("purchase_order_id", Kind.PURCHASE_ORDER),
                    ref("sales_order_id", Kind.SALES_ORDER),
                )
                if o
            ]
            older = ref("supersedes", Kind.COMMITMENT)
            com = entity(
                Kind.COMMITMENT,
                "commitment_id",
                label="Commitment",
                title=row.get("description"),
                props={
                    "commitment_type": ctype,
                    "status": status(Kind.COMMITMENT),
                    "due_date": _date(row, "due_date"),
                    **_money(row, "amount"),
                },
            )
            if older == com:
                raise _RowError("supersedes", "a commitment cannot supersede itself")
            if supplier or customer:
                link(Rel.OWED_TO, com, _must(supplier or customer), edge_source)
            for order in orders:
                link(Rel.FOR_ORDER, com, order, edge_source)
            if older:
                link(Rel.SUPERSEDES, com, older, edge_source)

        elif name == "dependencies.csv":
            com_ref = _must(ref("commitment_id", Kind.COMMITMENT, required=True))
            inv_ref = _must(ref("invoice_id", Kind.INVOICE, required=True))
            note = row.get("note")
            link(Rel.DEPENDS_ON, com_ref, inv_ref, edge_source, {"note": note} if note else {})
            if not sources:
                report.warnings.append(
                    ImportIssue(
                        file=name,
                        row=row_no,
                        column="source_ids",
                        message="dependency has no source document; it will be reported as "
                        "recorded but not documented",
                    )
                )

    def _ref(self, plan: _Plan, name: str, row_no: int, column: str, kind: Kind, raw: str) -> str:
        target = _business_id(kind, raw, column)
        plan.refs.append(_Ref(name, row_no, column, target, raw))
        return target

    # ------------------------------------------------------------ cross-row checks

    def _check_references(self, plan: _Plan, report: ImportReport) -> None:
        unresolved = sorted({r.target for r in plan.refs} - set(plan.defined))
        existing: set[str] = set()
        if unresolved and self.store.company_exists(self.company_id):
            rows = self.store.run(self.company_id, q.ENTITIES_EXIST, {"ids": unresolved})
            existing = {row["id"] for row in rows}
        for r in plan.refs:
            if r.target not in plan.defined and r.target not in existing:
                kind = r.target.split(":", 1)[0]
                report.errors.append(
                    ImportIssue(
                        file=r.file,
                        row=r.row,
                        column=r.column,
                        message=f"{kind} {r.raw} is not in this import or in the company's "
                        "existing records",
                    )
                )

    def _check_allocations(self, plan: _Plan, report: ImportReport) -> None:
        payments = {
            e.id: e
            for e in plan.entities
            if e.kind is Kind.PAYMENT and "amount_paise" in e.properties
        }
        allocated: dict[str, int] = defaultdict(int)
        for rel in plan.relationships:
            if rel.type is Rel.ALLOCATED_TO:
                allocated[rel.from_id] += int(rel.properties["amount_paise"] or 0)
        for pid, total in sorted(allocated.items()):
            payment = payments.get(pid)
            if payment is None:
                continue  # payment imported earlier; its amount is not re-checked here
            amount = int(payment.properties["amount_paise"] or 0)
            if total > amount:
                file, row = plan.defined[pid]
                report.errors.append(
                    ImportIssue(
                        file="allocations.csv",
                        column="amount",
                        message=f"allocations for {plan.raw_ids[pid]} total {_rupees(total)}, "
                        f"more than the payment amount {_rupees(amount)} ({file} row {row})",
                    )
                )
        # A payment applied to another customer's invoice is unusual but can be legitimate.
        billed = {r.from_id: r.to_id for r in plan.relationships if r.type is Rel.BILLED_TO}
        payer = {r.from_id: r.to_id for r in plan.relationships if r.type is Rel.RECEIVED_FROM}
        for rel in plan.relationships:
            if rel.type is not Rel.ALLOCATED_TO:
                continue
            a, b = payer.get(rel.from_id), billed.get(rel.to_id)
            if a and b and a != b:
                report.warnings.append(
                    ImportIssue(
                        file="allocations.csv",
                        message=f"{plan.raw_ids[rel.from_id]} was received from "
                        f"{plan.raw_ids.get(a, a)} but is allocated to "
                        f"{plan.raw_ids[rel.to_id]}, billed to {plan.raw_ids.get(b, b)}",
                    )
                )

    # ------------------------------------------------------------ writing

    def _write(self, plan: _Plan, report: ImportReport) -> None:
        ingest = Ingestor(self.store, self.company_id)
        try:
            for ent in plan.entities:
                if not ingest.upsert_entity(ent).applied:
                    report.ignored_stale.append(ent.id)
            for rel in plan.relationships:
                ingest.upsert_relationship(rel)
        except GraphError as exc:
            report.errors.append(
                ImportIssue(
                    file="(import)",
                    message=f"writing stopped part-way ({exc}). Writes are idempotent: fix the "
                    "cause and re-run the same import to complete it.",
                )
            )
            raise
        report.written = True


# ---------------------------------------------------------------- field helpers


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def _required(row: dict[str, str | None], column: str) -> str:
    value = row.get(column)
    if not value:
        raise _RowError(column, "required value is empty")
    return value


def _must(value: str | None) -> str:
    assert value is not None
    return value


def _business_id(kind: Kind, raw: str, column: str) -> str:
    if not _BUSINESS_ID_RE.fullmatch(raw):
        raise _RowError(
            column,
            f"{raw!r} is not a valid id: use letters, digits, '.', '_' or '-' (max 100), "
            "starting with a letter or digit",
        )
    return record_id(kind, raw)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date(row: dict[str, str | None], column: str) -> str | None:
    value = row.get(column)
    if value is None:
        return None
    try:
        if not _ISO_DATE_RE.fullmatch(value):
            raise ValueError(value)
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise _RowError(column, f"{value!r} is not a date; use YYYY-MM-DD") from None


def _timestamp(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _RowError(
            "updated_at", f"{value!r} is not a timestamp; use e.g. 2026-10-20T10:00:00Z"
        ) from None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _money(row: dict[str, str | None], column: str, *, required: bool = False) -> dict[str, Any]:
    """Parse a rupee amount into integer paise, plus its currency (INR only)."""
    currency = row.get("currency") or "INR"
    if currency.upper() != "INR":
        raise _RowError("currency", f"{currency!r} is not supported; amounts must be in INR")
    raw = row.get(column)
    if raw is None:
        if required:
            raise _RowError(column, "required value is empty")
        return {}
    text = raw.replace(",", "").replace("₹", "").removeprefix("Rs.").removeprefix("INR").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise _RowError(column, f"{raw!r} is not an amount; use e.g. 125000.50") from None
    if not amount.is_finite() or amount < 0:
        raise _RowError(column, f"{raw!r} must be zero or a positive amount")
    if amount != amount.quantize(Decimal("0.01")):
        raise _RowError(column, f"{raw!r} has more than two decimal places")
    paise = int(amount * 100)
    if paise > MAX_AMOUNT_PAISE:
        raise _RowError(column, f"{raw!r} is implausibly large")
    return {"amount_paise": paise, "currency": "INR"}


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    return str(err.get("msg", exc)).removeprefix("Value error, ")


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.2f}"
