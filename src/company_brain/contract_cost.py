"""Contract cost investigation for a small manufacturer, built on fixed graph queries.

Answers, without an LLM:

    How much has this contract cost so far, what is its provisional profitability,
    and what information is missing?

Calculation model (also returned as ``calculation_rules`` with every result):

* Recorded direct cost = the sum of the ``amount_paise`` on CHARGED_TO edges from
  valid (not void) purchase, wage, machinery and expense records into the contract or
  one of its jobs. Only the charged portion of a shared record counts.
* A charge with no amount is excluded and reported as unknown, never treated as zero.
* Costs the books only associate with the contract (NOTED_FOR, no amount) are pending
  allocation decisions. They are excluded from cost and reported with the amount that
  would still have to be split.
* Workshop overheads that are not charged to any contract are excluded by rule.
* Provisional profit = contract value - recorded direct cost. It is not accounting
  profit: no tax, depreciation, inventory or overhead absorption is applied.
* Supplier payments settle bills; customer receipts settle the contract value. Neither
  is a cost or additional revenue. Only cleared payments reduce what is owed.

All money is integer paise; percentages use ``Decimal``.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from company_brain import queries as q
from company_brain.cashflow import day, inr
from company_brain.graph import CompanyNotFound, GraphStore, Row
from company_brain.ingest import EntityNotFound
from company_brain.investigate import _edge, _Run
from company_brain.models import EvidencePath, PathEdge, SourceRef
from company_brain.schema import COST_KINDS

RESULT_LIMIT = 1000
COST_KIND_VALUES = sorted(k.value for k in COST_KINDS)
CATEGORY = {"purchase": "materials", "wage": "labour", "machinerycost": "machinery"}
_CONTRACT_REF_RE = re.compile(r"^(contract:)?([A-Za-z0-9][A-Za-z0-9._-]{0,99})$")

CALCULATION_RULES = [
    "Recorded direct cost = sum of the amounts charged (CHARGED_TO) to this contract or its "
    "jobs from valid purchase, labour, machinery and expense entries.",
    "A shared bill counts only for the portion charged here; the rest belongs to the other "
    "jobs or contracts it is charged to.",
    "Voided (struck-out) entries are excluded.",
    "Charges recorded without an amount are excluded and reported as unknown, not as zero.",
    "Costs only noted for this contract, without an amount, are excluded until someone "
    "decides the split.",
    "Workshop overheads not charged to any contract are excluded.",
    "Provisional profit = contract value - recorded direct cost. It is not final or "
    "statutory profit: no tax, depreciation, inventory valuation or overhead absorption "
    "is applied.",
    "Supplier payments and customer receipts settle amounts owed; they never add cost or "
    "revenue. Only cleared payments count as paid.",
]

FindingKind = Literal["fact", "calculation", "unknown", "inconsistency", "decision"]


def contract_id(ref: str) -> str:
    """``CT-104`` or ``contract:ct-104`` -> ``contract:ct-104``."""
    match = _CONTRACT_REF_RE.fullmatch(ref.strip())
    if not match:
        raise ValueError(f"{ref!r} is not a contract reference such as CT-104")
    return f"contract:{match[2].lower()}"


def percent(part: int, whole: int) -> str | None:
    if not whole:
        return None
    value = Decimal(part) * 100 / Decimal(whole)
    return str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _ref(record: dict[str, Any]) -> str:
    return str(record.get("source_record_id") or record["id"])


def _when(record: dict[str, Any]) -> str | None:
    for key in ("purchase_date", "work_date", "cost_date", "expense_date"):
        if record.get(key):
            return str(record[key])
    return None


# ---------------------------------------------------------------- models


class ContractCostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: str = Field(examples=["CT-104"], description="Contract reference or id.")
    as_of: date | None = Field(default=None, description="Reporting date. Defaults to today.")


class Job(BaseModel):
    id: str
    ref: str
    status: str | None
    started_on: str | None = None
    completed_on: str | None = None


class ContractSummary(BaseModel):
    id: str
    ref: str
    name: str | None
    customer_id: str | None
    customer_name: str | None
    status: str | None
    body_type: str | None
    body_count: int | None
    contract_date: str | None
    expected_completion: str | None
    value_paise: int | None
    jobs: list[Job]
    source_ids: list[str]


class CostLine(BaseModel):
    record_id: str
    ref: str
    kind: str
    category: str
    description: str | None
    date: str | None
    charged_to: str
    charged_to_ref: str | None
    charged_paise: int | None
    record_amount_paise: int | None
    shared_with: list[str] = Field(
        description="Jobs or contracts outside this contract that the same entry is charged to."
    )
    counterparty: str | None
    source_ids: list[str]
    path_id: str


class CostBreakdown(BaseModel):
    materials_paise: int
    labour_paise: int
    machinery_paise: int
    other_paise: int
    total_paise: int
    entries: int = Field(description="Distinct book entries charged to this contract.")
    charges: int = Field(description="Charge lines; a bill split across bodies counts per body.")
    excluded_void: list[str]
    excluded_without_amount: list[str]


class PendingAllocation(BaseModel):
    record_id: str
    ref: str
    kind: str
    description: str | None
    amount_paise: int | None
    unallocated_paise: int | None
    noted_for: list[str]
    source_ids: list[str]


class Profitability(BaseModel):
    contract_value_paise: int | None
    recorded_direct_cost_paise: int
    provisional_profit_paise: int | None
    provisional_margin_percent: str | None
    pending_allocation_paise: int = Field(
        description="Unsplit costs noted for this contract (and possibly others)."
    )
    profit_if_all_pending_charged_here_paise: int | None = Field(
        description="Lower bound: provisional profit if every pending item were charged "
        "entirely to this contract. A sensitivity, not an allocation."
    )
    label: str = "provisional, based on recorded direct costs only"


class CustomerAccount(BaseModel):
    contract_value_paise: int | None
    received_paise: int = Field(description="Cleared customer receipts against this contract.")
    pending_receipts_paise: int
    outstanding_paise: int | None
    receipts: list[dict[str, Any]]


class SupplierBill(BaseModel):
    record_id: str
    ref: str
    supplier: str
    bill_amount_paise: int | None
    charged_here_paise: int
    paid_cleared_paise: int
    paid_pending_paise: int
    outstanding_paise: int | None
    book_paid_status: str | None
    reconciliation: Literal["consistent", "unconfirmed", "mismatch", "not_recorded"]
    settled_in: Literal["supplier_payment_book", "expense_book"] = Field(
        description="Where payment of this cost is recorded. Petty expenses are paid from "
        "the expense book itself, so their outstanding amount follows that book."
    )
    shared_with: list[str]


class ContractFinding(BaseModel):
    id: str
    kind: FindingKind = Field(
        description="fact: recorded and backed by a source. calculation: derived by the "
        "rules in calculation_rules. unknown: not recorded. inconsistency: records disagree. "
        "decision: a person must decide before the figure can be completed."
    )
    category: str
    statement: str
    entity_ids: list[str]
    path_ids: list[str] = []
    source_ids: list[str] = []


class ContractCostResponse(BaseModel):
    investigation_id: str
    company_id: str
    as_of: date
    contract: ContractSummary
    cost_breakdown: CostBreakdown
    profitability: Profitability
    customer_account: CustomerAccount
    supplier_bills: list[SupplierBill]
    wages_unpaid: list[dict[str, Any]]
    cost_lines: list[CostLine]
    pending_allocations: list[PendingAllocation]
    findings: list[ContractFinding]
    decisions_required: list[str]
    missing_information: list[str]
    warnings: list[str]
    calculation_rules: list[str]
    evidence_paths: list[EvidencePath]
    sources: list[SourceRef]
    queries_executed: list[str]


class UnallocatedItem(BaseModel):
    record_id: str
    ref: str
    kind: str
    description: str | None
    date: str | None
    amount_paise: int | None
    charged_paise: int
    unallocated_paise: int | None
    noted_for: list[str]
    counterparty: str | None
    source_ids: list[str]
    unlinked_targets: list[str] = Field(
        default=[], description="Jobs it is charged to that are not linked to any contract."
    )
    classification: Literal["needs_split", "unlinked", "unassigned", "overhead"]


class UnallocatedResponse(BaseModel):
    company_id: str
    items: list[UnallocatedItem]
    total_unallocated_paise: int
    unapplied_payments: list[dict[str, Any]]
    queries_executed: list[str]


class ContractOverviewRow(BaseModel):
    ref: str
    id: str
    customer: str | None
    status: str | None
    value_paise: int | None
    recorded_cost_paise: int
    provisional_profit_paise: int | None
    provisional_margin_percent: str | None
    received_paise: int
    outstanding_receivable_paise: int | None
    pending_allocations: list[str]
    issues: list[str]


class OverviewResponse(BaseModel):
    company_id: str
    contracts: list[ContractOverviewRow]
    unallocated_overhead_paise: int
    queries_executed: list[str]
    calculation_rules: list[str]


# ---------------------------------------------------------------- investigation


@dataclass
class _ContractRun(_Run):
    contract_findings: list[ContractFinding] = field(default_factory=list)

    def add_finding(self, **kwargs: Any) -> None:
        self.contract_findings.append(
            ContractFinding(id=f"finding-{len(self.contract_findings) + 1}", **kwargs)
        )


class ContractCostInvestigator:
    def __init__(self, store: GraphStore, company_id: str) -> None:
        self.store = store
        self.company_id = company_id

    def _require_company(self) -> None:
        if not self.store.company_exists(self.company_id):
            raise CompanyNotFound(self.company_id)

    # ------------------------------------------------------------ primary

    def investigate(self, request: ContractCostRequest) -> ContractCostResponse:
        cid = contract_id(request.contract)
        self._require_company()
        as_of = request.as_of or date.today()
        run = _ContractRun(self.store, self.company_id)
        decisions: list[str] = []

        rows = run.run(q.MF_CONTRACT, {"id": cid})
        if not rows:
            raise EntityNotFound([cid])
        summary = self._summary(rows[0])
        in_scope = {cid} | {j.id for j in summary.jobs}

        charge_rows = run.run(
            q.MF_CONTRACT_CHARGES,
            {"id": cid, "cost_kinds": COST_KIND_VALUES, "limit": RESULT_LIMIT},
        )
        if len(charge_rows) == RESULT_LIMIT:
            run.warnings.append(f"Only the first {RESULT_LIMIT} cost entries were examined.")
        graph_totals = {
            r["kind"]: r
            for r in run.run(q.MF_CONTRACT_COST_TOTALS, {"id": cid, "cost_kinds": COST_KIND_VALUES})
        }
        noted_rows = run.run(
            q.MF_CONTRACT_NOTED,
            {"id": cid, "cost_kinds": COST_KIND_VALUES, "limit": RESULT_LIMIT},
        )
        unallocated_rows = run.run(
            q.MF_UNALLOCATED, {"cost_kinds": COST_KIND_VALUES, "limit": RESULT_LIMIT}
        )

        # -- contract facts -------------------------------------------------------
        run.add_finding(
            kind="fact",
            category="contract",
            statement=f"{summary.ref} for {summary.customer_name}: {summary.body_count} "
            f"{summary.body_type} bod{'ies' if (summary.body_count or 0) != 1 else 'y'}, "
            f"{inr(summary.value_paise)}, contracted {day(summary.contract_date)}, expected "
            f"by {day(summary.expected_completion)}; status {summary.status}. Jobs: "
            + ", ".join(f"{j.ref} ({j.status})" for j in summary.jobs)
            + ".",
            entity_ids=[cid, *(j.id for j in summary.jobs)],
            source_ids=summary.source_ids,
        )
        if not summary.source_ids:
            run.missing.append(f"No source record supports contract {summary.ref}.")
        if summary.value_paise is None:
            run.missing.append(f"No contract value is recorded for {summary.ref}.")

        # -- cost lines -------------------------------------------------------------
        lines, breakdown = self._cost_lines(run, charge_rows, in_scope)
        self._check_graph_totals(run, breakdown, graph_totals)
        for ref in breakdown.excluded_void:
            run.add_finding(
                kind="fact",
                category="voided_entry",
                statement=f"{ref} is struck out in the book (void); it is not counted.",
                entity_ids=[r["record"]["id"] for r in charge_rows if _ref(r["record"]) == ref][:1],
            )
        for ref in breakdown.excluded_without_amount:
            run.add_finding(
                kind="unknown",
                category="charge_without_amount",
                statement=f"{ref} is charged to this contract without an amount; it is "
                "excluded from cost until the amount is recorded.",
                entity_ids=[],
            )
        self._over_allocation(run, charge_rows)
        self._shared_lines(run, lines)
        for line in lines:
            if not line.source_ids:
                run.missing.append(f"No source record supports {line.ref} ({line.description}).")

        run.add_finding(
            kind="calculation",
            category="recorded_direct_cost",
            statement=f"Recorded direct cost so far: {inr(breakdown.total_paise)} from "
            f"{breakdown.entries} book entries ({breakdown.charges} charges, as some are split "
            f"across bodies): materials {inr(breakdown.materials_paise)}, "
            f"labour {inr(breakdown.labour_paise)}, machinery "
            f"{inr(breakdown.machinery_paise)}, other direct {inr(breakdown.other_paise)}.",
            entity_ids=[cid],
            path_ids=sorted({ln.path_id for ln in lines}),
        )

        # -- pending allocations and overheads --------------------------------------
        pending = self._pending(noted_rows)
        for item in pending:
            others = [t for t in item.noted_for if t not in in_scope]
            run.add_finding(
                kind="decision",
                category="allocation_needed",
                statement=f"{item.ref} ({item.description}, {inr(item.amount_paise)}) is noted "
                f"for {', '.join(_short(t) for t in item.noted_for)} with no split recorded. "
                f"{inr(item.unallocated_paise)} is not counted in any contract's cost until "
                "the split is decided.",
                entity_ids=[item.record_id, *item.noted_for],
                source_ids=item.source_ids,
            )
            decisions.append(
                f"Decide how much of {item.ref} ({inr(item.unallocated_paise)}) belongs to "
                f"{summary.ref}" + (f" versus {', '.join(map(_short, others))}." if others else ".")
            )
        pending_paise = sum(p.unallocated_paise or 0 for p in pending)
        unlinked = [r for r in unallocated_rows if not r["noted_targets"] and r["unlinked_targets"]]
        for r in unlinked:
            jobs = ", ".join(_short(t) for t in sorted(r["unlinked_targets"]))
            run.missing.append(
                f"{_ref(r['record'])} is charged to {jobs}, which is not linked to any contract; "
                f"{inr(int(r['record'].get('amount_paise') or 0) - int(r['charged_paise'] or 0))}"
                " of it is not counted in any contract."
            )
        overheads = [
            r
            for r in unallocated_rows
            if not r["noted_targets"]
            and not r["unlinked_targets"]
            and r["kind"] != "wage"
            and r["record"].get("amount_paise")
        ]
        unassigned_labour = [
            r
            for r in unallocated_rows
            if not r["noted_targets"] and not r["unlinked_targets"] and r["kind"] == "wage"
        ]
        for r in unassigned_labour:
            rec = r["record"]
            who = ", ".join(w["name"] for w in r["workers"]) or "an unrecorded worker"
            run.add_finding(
                kind="unknown",
                category="unassigned_labour",
                statement=f"{_ref(rec)}: {who}, {rec.get('name')}, {inr(rec.get('amount_paise'))}"
                " has no job recorded. It may belong to this or another contract; it is not "
                "counted anywhere.",
                entity_ids=[rec["id"]],
                source_ids=r["source_ids"],
            )
            decisions.append(f"Record which job {_ref(rec)} ({who}) worked on.")
        if overheads:
            total = sum(int(r["record"]["amount_paise"]) for r in overheads)
            refs = ", ".join(
                f"{_ref(r['record'])} {inr(r['record']['amount_paise'])}" for r in overheads
            )
            run.add_finding(
                kind="decision",
                category="unallocated_overheads",
                statement=f"{inr(total)} of workshop costs are not charged to any contract "
                f"({refs}). They are excluded from every contract's direct cost; decide whether "
                "and how to share them.",
                entity_ids=[r["record"]["id"] for r in overheads],
                source_ids=sorted({s for r in overheads for s in r["source_ids"]}),
            )
            decisions.append("Decide a rule for sharing workshop overheads across contracts.")
            for r in overheads:
                if not r["source_ids"]:
                    run.missing.append(
                        f"No source record supports {_ref(r['record'])} "
                        f"({r['record'].get('name')})."
                    )

        # -- profitability ------------------------------------------------------------
        value = summary.value_paise
        profit = value - breakdown.total_paise if value is not None else None
        profitability = Profitability(
            contract_value_paise=value,
            recorded_direct_cost_paise=breakdown.total_paise,
            provisional_profit_paise=profit,
            provisional_margin_percent=percent(profit, value)
            if profit is not None and value
            else None,
            pending_allocation_paise=pending_paise,
            profit_if_all_pending_charged_here_paise=(
                profit - pending_paise if profit is not None else None
            ),
        )
        if profit is not None:
            run.add_finding(
                kind="calculation",
                category="provisional_profit",
                statement=f"Provisional profit based on recorded direct costs: {inr(profit)} "
                f"({profitability.provisional_margin_percent}% of {inr(value)}). "
                + (
                    f"If all {inr(pending_paise)} of unsplit costs noted for it were charged "
                    f"here, it would be {inr(profit - pending_paise)}. "
                    if pending_paise
                    else ""
                )
                + "Not final profit: overheads, unassigned labour and future costs are not "
                "included.",
                entity_ids=[cid],
            )
        if summary.status not in {"completed", "delivered"}:
            run.warnings.append(
                f"{summary.ref} is still {summary.status}: costs still to come are not in "
                "the records, so profit will change."
            )

        # -- customer account -----------------------------------------------------------
        account = self._customer_account(run, summary, rows[0]["receipts"])

        # -- supplier bills and wages ---------------------------------------------------
        bills = self._supplier_bills(run, charge_rows, in_scope, decisions)
        self._unapplied_payments(run, charge_rows, bills, decisions)
        wages_unpaid = self._wages(run, charge_rows)

        # -- sources --------------------------------------------------------------------
        sources = self._sources(run, cid, charge_rows, noted_rows, unallocated_rows, rows[0])

        run.warnings.append(
            "Figures are provisional and based only on the recorded entries. They are not "
            "statutory accounts: no GST, depreciation, inventory valuation or overhead "
            "absorption is applied. Confirm with the accountant before relying on them."
        )
        return ContractCostResponse(
            investigation_id=f"ccr_{uuid.uuid4().hex[:16]}",
            company_id=self.company_id,
            as_of=as_of,
            contract=summary,
            cost_breakdown=breakdown,
            profitability=profitability,
            customer_account=account,
            supplier_bills=bills,
            wages_unpaid=wages_unpaid,
            cost_lines=lines,
            pending_allocations=pending,
            findings=run.contract_findings,
            decisions_required=list(dict.fromkeys(decisions)),
            missing_information=list(dict.fromkeys(run.missing)),
            warnings=list(dict.fromkeys(run.warnings)),
            calculation_rules=CALCULATION_RULES,
            evidence_paths=run.paths,
            sources=sources,
            queries_executed=run.executed,
        )

    # ------------------------------------------------------------ steps

    def _summary(self, row: Row) -> ContractSummary:
        c = row["contract"]
        customer = row["customers"][0] if row["customers"] else {}
        return ContractSummary(
            id=c["id"],
            ref=_ref(c),
            name=c.get("name"),
            customer_id=customer.get("id"),
            customer_name=customer.get("name"),
            status=c.get("status"),
            body_type=c.get("body_type"),
            body_count=c.get("body_count"),
            contract_date=c.get("contract_date"),
            expected_completion=c.get("expected_completion"),
            value_paise=c.get("amount_paise"),
            jobs=[
                Job(
                    id=j["id"],
                    ref=j["ref"] or j["id"],
                    status=j.get("status"),
                    started_on=j.get("started_on"),
                    completed_on=j.get("completed_on"),
                )
                for j in sorted(row["jobs"], key=lambda j: j["id"])
            ],
            source_ids=row["source_ids"],
        )

    def _cost_lines(
        self, run: _ContractRun, charge_rows: list[Row], in_scope: set[str]
    ) -> tuple[list[CostLine], CostBreakdown]:
        lines: list[CostLine] = []
        totals: dict[str, int] = defaultdict(int)
        void: list[str] = []
        no_amount: list[str] = []
        for row in charge_rows:
            rec = row["record"]
            ref = _ref(rec)
            if rec.get("status") == "void":
                void.append(ref)
                continue
            if row["charged_paise"] is None:
                no_amount.append(ref)
                continue
            category = CATEGORY.get(row["kind"], "other")
            counterparty = ", ".join(
                [s["name"] for s in row["suppliers"]] + [w["name"] for w in row["workers"]]
            )
            edges = [_edge(r) for r in row["rels"]]
            edges += [
                PathEdge(type="SUPPLIED_BY", from_id=rec["id"], to_id=s["id"])
                for s in row["suppliers"]
            ] + [
                PathEdge(type="EARNED_BY", from_id=rec["id"], to_id=w["id"]) for w in row["workers"]
            ]
            path_id = run.add_path(f"{ref} charged to {row['charged_to_ref']}", edges)
            lines.append(
                CostLine(
                    record_id=rec["id"],
                    ref=ref,
                    kind=row["kind"],
                    category=category,
                    description=rec.get("name"),
                    date=_when(rec),
                    charged_to=row["charged_to"],
                    charged_to_ref=row["charged_to_ref"],
                    charged_paise=int(row["charged_paise"]),
                    record_amount_paise=rec.get("amount_paise"),
                    shared_with=sorted(
                        c["id"] for c in row["all_charges"] if c["id"] not in in_scope
                    ),
                    counterparty=counterparty or None,
                    source_ids=row["source_ids"],
                    path_id=path_id,
                )
            )
            totals[category] += int(row["charged_paise"])
        breakdown = CostBreakdown(
            materials_paise=totals["materials"],
            labour_paise=totals["labour"],
            machinery_paise=totals["machinery"],
            other_paise=totals["other"],
            total_paise=sum(totals.values()),
            entries=len({ln.record_id for ln in lines}),
            charges=len(lines),
            excluded_void=sorted(set(void)),
            excluded_without_amount=sorted(set(no_amount)),
        )
        return lines, breakdown

    def _check_graph_totals(
        self, run: _ContractRun, breakdown: CostBreakdown, graph_totals: dict[str, Row]
    ) -> None:
        by_category: dict[str, int] = defaultdict(int)
        for kind, row in graph_totals.items():
            by_category[CATEGORY.get(kind, "other")] += int(row["charged_paise"] or 0)
        mine = {
            "materials": breakdown.materials_paise,
            "labour": breakdown.labour_paise,
            "machinery": breakdown.machinery_paise,
            "other": breakdown.other_paise,
        }
        if any(by_category[k] != v for k, v in mine.items()):
            run.warnings.append(
                "The graph's category totals differ from the itemised entries; the result "
                "may be incomplete (more entries than the query limit?)."
            )

    def _over_allocation(self, run: _ContractRun, charge_rows: list[Row]) -> None:
        seen: set[str] = set()
        for row in charge_rows:
            rec = row["record"]
            if rec["id"] in seen or rec.get("status") == "void":
                continue
            seen.add(rec["id"])
            charged = sum(int(c["amount_paise"] or 0) for c in row["all_charges"])
            amount = rec.get("amount_paise")
            if amount is not None and charged > amount:
                run.add_finding(
                    kind="inconsistency",
                    category="over_allocated",
                    statement=f"{_ref(rec)} is charged {inr(charged)} in total across jobs and "
                    f"contracts, more than its recorded amount {inr(amount)}. Some cost may be "
                    "counted twice.",
                    entity_ids=[rec["id"], *(c["id"] for c in row["all_charges"])],
                    source_ids=row["source_ids"],
                )
            elif amount is None:
                run.missing.append(f"{_ref(rec)} has no amount recorded.")

    def _shared_lines(self, run: _ContractRun, lines: list[CostLine]) -> None:
        by_record: dict[str, list[CostLine]] = defaultdict(list)
        for line in lines:
            by_record[line.record_id].append(line)
        for record_lines in by_record.values():
            first = record_lines[0]
            here = sum(ln.charged_paise or 0 for ln in record_lines)
            if not first.shared_with:
                continue
            run.add_finding(
                kind="fact",
                category="shared_cost",
                statement=f"{first.ref} ({first.description}, {inr(first.record_amount_paise)}) "
                f"is shared: {inr(here)} is charged to "
                + ", ".join(_short(ln.charged_to) for ln in record_lines)
                + f" and the rest to {', '.join(map(_short, first.shared_with))}. Only "
                f"{inr(here)} counts here.",
                entity_ids=[first.record_id, *(ln.charged_to for ln in record_lines)],
                path_ids=[ln.path_id for ln in record_lines],
                source_ids=first.source_ids,
            )

    def _pending(self, noted_rows: list[Row]) -> list[PendingAllocation]:
        items = []
        for row in noted_rows:
            rec = row["record"]
            amount = rec.get("amount_paise")
            charged = sum(int(c["amount_paise"] or 0) for c in row["all_charges"])
            if amount is not None and charged >= amount:
                continue  # the split has since been recorded
            items.append(
                PendingAllocation(
                    record_id=rec["id"],
                    ref=_ref(rec),
                    kind=row["kind"],
                    description=rec.get("name"),
                    amount_paise=amount,
                    unallocated_paise=amount - charged if amount is not None else None,
                    noted_for=sorted(row["noted_for"]),
                    source_ids=sorted({*row["source_ids"], *filter(None, row["note_sources"])}),
                )
            )
        return items

    def _customer_account(
        self, run: _ContractRun, summary: ContractSummary, receipts: list[dict[str, Any]]
    ) -> CustomerAccount:
        cleared = sum(int(r["amount_paise"] or 0) for r in receipts if r["status"] == "cleared")
        pending = sum(int(r["amount_paise"] or 0) for r in receipts if r["status"] == "pending")
        value = summary.value_paise
        account = CustomerAccount(
            contract_value_paise=value,
            received_paise=cleared,
            pending_receipts_paise=pending,
            outstanding_paise=value - cleared if value is not None else None,
            receipts=sorted(receipts, key=lambda r: r["id"]),
        )
        run.add_finding(
            kind="fact",
            category="customer_receipts",
            statement=f"{summary.customer_name} has paid {inr(cleared)} against {summary.ref} "
            f"in {sum(r['status'] == 'cleared' for r in receipts)} cleared receipt(s); "
            f"{inr(account.outstanding_paise)} of the contract value is still to be received. "
            "Receipts are cash collected, not additional revenue.",
            entity_ids=[summary.id, *(r["id"] for r in receipts)],
            source_ids=sorted({r["source_id"] for r in receipts if r.get("source_id")}),
        )
        if pending:
            run.add_finding(
                kind="unknown",
                category="receipt_unconfirmed",
                statement=f"{inr(pending)} of customer receipts are recorded but not cleared.",
                entity_ids=[r["id"] for r in receipts if r["status"] == "pending"],
            )
        return account

    def _supplier_bills(
        self, run: _ContractRun, charge_rows: list[Row], in_scope: set[str], decisions: list[str]
    ) -> list[SupplierBill]:
        rows_by_record: dict[str, list[Row]] = defaultdict(list)
        for row in charge_rows:
            if row["record"].get("status") != "void" and row["suppliers"]:
                rows_by_record[row["record"]["id"]].append(row)
        bills = []
        for rows in rows_by_record.values():
            row = rows[0]
            rec = row["record"]
            ref = _ref(rec)
            amount = rec.get("amount_paise")
            cleared = sum(
                int(p["amount_paise"] or 0) for p in row["payments"] if p["status"] == "cleared"
            )
            pending = sum(
                int(p["amount_paise"] or 0) for p in row["payments"] if p["status"] == "pending"
            )
            book = rec.get("paid_status")
            settled_in: Literal["supplier_payment_book", "expense_book"] = (
                "expense_book" if row["kind"] == "expense" else "supplier_payment_book"
            )
            if settled_in == "expense_book":
                outstanding = {"paid": 0, "unpaid": amount}.get(book or "")
            else:
                outstanding = amount - cleared if amount is not None else None
            reconciliation = self._reconcile(row["kind"], book, amount, cleared, pending)
            shared = sorted(c["id"] for c in row["all_charges"] if c["id"] not in in_scope)
            supplier = ", ".join(s["name"] for s in row["suppliers"])
            bills.append(
                SupplierBill(
                    record_id=rec["id"],
                    ref=ref,
                    supplier=supplier,
                    bill_amount_paise=amount,
                    charged_here_paise=sum(int(r["charged_paise"] or 0) for r in rows),
                    paid_cleared_paise=cleared,
                    paid_pending_paise=pending,
                    outstanding_paise=outstanding,
                    book_paid_status=book,
                    reconciliation=reconciliation,
                    settled_in=settled_in,
                    shared_with=shared,
                )
            )
            pay_refs = ", ".join(p["ref"] or p["id"] for p in row["payments"]) or "none"
            if reconciliation == "unconfirmed":
                run.add_finding(
                    kind="unknown",
                    category="payment_unconfirmed",
                    statement=f"{ref} ({supplier}, {inr(amount)}): payment {pay_refs} of "
                    f"{inr(pending)} is recorded but not cleared, so the bill is not yet "
                    "confirmed as paid.",
                    entity_ids=[rec["id"], *(p["id"] for p in row["payments"])],
                    source_ids=sorted(
                        {p["source_id"] for p in row["payments"] if p.get("source_id")}
                    ),
                )
            elif reconciliation == "mismatch":
                run.add_finding(
                    kind="inconsistency",
                    category="unreconciled",
                    statement=f"{ref} ({supplier}, {inr(amount)}): the purchase book says "
                    f"'{book}', but the supplier payment book shows {inr(cleared)} cleared "
                    f"against it (payments: {pay_refs}).",
                    entity_ids=[rec["id"], *(p["id"] for p in row["payments"])],
                    source_ids=row["source_ids"],
                )
                decisions.append(f"Reconcile {ref} with the supplier payment book.")
            elif reconciliation == "not_recorded" and row["kind"] in {"purchase", "machinerycost"}:
                run.missing.append(f"The book does not say whether {ref} ({supplier}) was paid.")
        owed = [b for b in bills if b.outstanding_paise]
        if owed:
            run.add_finding(
                kind="calculation",
                category="supplier_outstanding",
                statement="Supplier bills on this contract with money still owed per payment "
                "records: "
                + "; ".join(
                    f"{b.ref} {b.supplier} {inr(b.outstanding_paise)} of {inr(b.bill_amount_paise)}"
                    + (
                        " (whole bill, shared with " + ", ".join(map(_short, b.shared_with)) + ")"
                        if b.shared_with
                        else ""
                    )
                    for b in owed
                )
                + ". These are amounts owed, not extra cost.",
                entity_ids=[b.record_id for b in owed],
            )
        return sorted(bills, key=lambda b: b.ref)

    @staticmethod
    def _reconcile(
        kind: str, book: str | None, amount: int | None, cleared: int, pending: int
    ) -> Literal["consistent", "unconfirmed", "mismatch", "not_recorded"]:
        if kind == "expense":
            # Petty expenses are settled in the expense book itself, not the supplier
            # payment book, so there is nothing to reconcile against.
            return "not_recorded" if book is None else "consistent"
        if book is None:
            return "not_recorded"
        fully = amount is not None and cleared >= amount
        if book == "paid":
            if fully:
                return "consistent"
            return (
                "unconfirmed" if amount is not None and cleared + pending >= amount else "mismatch"
            )
        if book == "unpaid":
            return "consistent" if cleared == 0 else "mismatch"
        # partly_paid
        return "consistent" if cleared > 0 and not fully else "mismatch"

    def _unapplied_payments(
        self,
        run: _ContractRun,
        charge_rows: list[Row],
        bills: list[SupplierBill],
        decisions: list[str],
    ) -> None:
        supplier_ids = sorted({s["id"] for r in charge_rows for s in r["suppliers"]})
        if not supplier_ids:
            return
        for row in run.run(q.MF_UNAPPLIED_PAYMENTS, {"supplier_ids": supplier_ids}):
            payment = row["payment"]
            unapplied = int(payment["amount_paise"]) - int(row["applied_paise"] or 0)
            open_bills = [
                b
                for b in bills
                if b.supplier == row["supplier_name"] and (b.outstanding_paise or 0) > 0
            ]
            run.add_finding(
                kind="decision",
                category="unapplied_payment",
                statement=f"{_ref(payment)}: {inr(unapplied)} paid to {row['supplier_name']} on "
                f"{day(payment.get('paid_on'))} is not applied to any bill. Open bills from this "
                "supplier on this contract: "
                + (
                    ", ".join(f"{b.ref} ({inr(b.outstanding_paise)} unpaid)" for b in open_bills)
                    or "none"
                )
                + ". Which bill it settles is not recorded.",
                entity_ids=[row["id"], row["supplier_id"], *(b.record_id for b in open_bills)],
                source_ids=row["source_ids"],
            )
            decisions.append(f"Record which bill {_ref(payment)} ({inr(unapplied)}) pays.")

    def _wages(self, run: _ContractRun, charge_rows: list[Row]) -> list[dict[str, Any]]:
        unpaid = []
        for row in charge_rows:
            rec = row["record"]
            if row["kind"] != "wage" or rec.get("status") == "void":
                continue
            who = ", ".join(w["name"] for w in row["workers"])
            if rec.get("paid_status") is None:
                run.add_finding(
                    kind="unknown",
                    category="wage_payment_unknown",
                    statement=f"{_ref(rec)} ({who}, {inr(rec.get('amount_paise'))}): the wage "
                    "book does not say whether it was paid. It is counted as cost; whether it "
                    "is still owed is unknown.",
                    entity_ids=[rec["id"]],
                    source_ids=row["source_ids"],
                )
            elif rec.get("paid_status") != "paid":
                unpaid.append(
                    {
                        "record_id": rec["id"],
                        "ref": _ref(rec),
                        "worker": who,
                        "amount_paise": rec.get("amount_paise"),
                        "period": f"{rec.get('work_date')} to {rec.get('period_end')}",
                        "paid_status": rec.get("paid_status"),
                    }
                )
        if unpaid:
            run.add_finding(
                kind="fact",
                category="wages_unpaid",
                statement="Wages charged to this contract and recorded as unpaid: "
                + "; ".join(f"{w['ref']} {w['worker']} {inr(w['amount_paise'])}" for w in unpaid)
                + ". They are already in cost; paying them adds no cost.",
                entity_ids=[w["record_id"] for w in unpaid],
            )
        return unpaid

    def _sources(
        self,
        run: _ContractRun,
        cid: str,
        charge_rows: list[Row],
        noted_rows: list[Row],
        unallocated_rows: list[Row],
        contract: Row,
    ) -> list[SourceRef]:
        ids = sorted(
            {cid}
            | {r["record"]["id"] for r in charge_rows}
            | {r["record"]["id"] for r in noted_rows}
            | {r["record"]["id"] for r in unallocated_rows}
            | {p["id"] for r in charge_rows for p in r["payments"]}
            | {p["id"] for p in contract["receipts"]}
        )
        sources = [
            SourceRef(relation=row["relation"], about=row["entity_id"], **row["source"])
            for row in run.run(q.SOURCES_FOR, {"ids": ids})
        ]
        cited = {s.id for s in sources}
        edge_sources = sorted(
            (
                {r["charge_source_id"] for r in charge_rows if r.get("charge_source_id")}
                | {s for r in noted_rows for s in r["note_sources"] if s}
            )
            - cited
        )
        if edge_sources:
            for row in run.run(q.CF_SOURCES_BY_ID, {"ids": edge_sources}):
                sources.append(SourceRef(relation="asserts", about="allocation", **row["source"]))
        for s in sources:
            if s.status != "active":
                run.warnings.append(f"{s.id} was {s.status}; it is not counted as evidence.")
        return sorted(sources, key=lambda s: (s.about, s.relation, s.id))

    # ------------------------------------------------------------ secondary

    def unallocated(self) -> UnallocatedResponse:
        """Which recorded costs are not (fully) charged to any job or contract?"""
        self._require_company()
        run = _ContractRun(self.store, self.company_id)
        rows = run.run(q.MF_UNALLOCATED, {"cost_kinds": COST_KIND_VALUES, "limit": RESULT_LIMIT})
        items = []
        for row in rows:
            rec = row["record"]
            amount = rec.get("amount_paise")
            charged = int(row["charged_paise"] or 0)
            noted = [t["ref"] or t["id"] for t in row["noted_targets"]]
            classification: Literal["needs_split", "unlinked", "unassigned", "overhead"] = (
                "needs_split"
                if noted
                else "unlinked"
                if row["unlinked_targets"]
                else "unassigned"
                if row["kind"] == "wage"
                else "overhead"
            )
            items.append(
                UnallocatedItem(
                    record_id=rec["id"],
                    ref=_ref(rec),
                    kind=row["kind"],
                    description=rec.get("name"),
                    date=_when(rec),
                    amount_paise=amount,
                    charged_paise=charged,
                    unallocated_paise=amount - charged if amount is not None else None,
                    noted_for=sorted(noted),
                    counterparty=", ".join(
                        [s["name"] for s in row["suppliers"]] + [w["name"] for w in row["workers"]]
                    )
                    or None,
                    source_ids=row["source_ids"],
                    unlinked_targets=sorted(row["unlinked_targets"]),
                    classification=classification,
                )
            )
        unapplied = [
            {
                "payment_id": r["id"],
                "ref": _ref(r["payment"]),
                "supplier": r["supplier_name"],
                "paid_on": r["payment"].get("paid_on"),
                "unapplied_paise": int(r["payment"]["amount_paise"]) - int(r["applied_paise"] or 0),
            }
            for r in run.run(q.MF_UNAPPLIED_PAYMENTS, {"supplier_ids": None})
        ]
        return UnallocatedResponse(
            company_id=self.company_id,
            items=items,
            total_unallocated_paise=sum(i.unallocated_paise or 0 for i in items),
            unapplied_payments=unapplied,
            queries_executed=run.executed,
        )

    def overview(self) -> OverviewResponse:
        """Which contracts have incomplete cost records, and how do they stand?"""
        self._require_company()
        run = _ContractRun(self.store, self.company_id)
        rows = run.run(q.MF_CONTRACTS_OVERVIEW, {"limit": RESULT_LIMIT})
        unallocated = run.run(
            q.MF_UNALLOCATED, {"cost_kinds": COST_KIND_VALUES, "limit": RESULT_LIMIT}
        )
        contracts = []
        for row in rows:
            c = row["contract"]
            scope = {c["id"], *row["job_ids"]}
            pending = sorted(
                _ref(u["record"])
                for u in unallocated
                if {t["id"] for t in u["noted_targets"]} & scope
            )
            value = c.get("amount_paise")
            cost = int(row["recorded_cost_paise"] or 0)
            received = int(row["received_paise"] or 0)
            profit = value - cost if value is not None else None
            issues = []
            if pending:
                issues.append(f"{len(pending)} cost(s) noted for it await a split")
            if row["charges_without_amount"]:
                issues.append(f"{row['charges_without_amount']} charge(s) without an amount")
            if c.get("status") not in {"completed", "delivered"}:
                issues.append("still in production: costs incomplete")
            contracts.append(
                ContractOverviewRow(
                    ref=_ref(c),
                    id=c["id"],
                    customer=", ".join(row["customers"]) or None,
                    status=c.get("status"),
                    value_paise=value,
                    recorded_cost_paise=cost,
                    provisional_profit_paise=profit,
                    provisional_margin_percent=percent(profit, value)
                    if profit is not None and value
                    else None,
                    received_paise=received,
                    outstanding_receivable_paise=value - received if value is not None else None,
                    pending_allocations=pending,
                    issues=issues,
                )
            )
        overhead = sum(
            int(u["record"]["amount_paise"] or 0)
            for u in unallocated
            if not u["noted_targets"] and not u["unlinked_targets"] and u["kind"] != "wage"
        )
        return OverviewResponse(
            company_id=self.company_id,
            contracts=contracts,
            unallocated_overhead_paise=overhead,
            queries_executed=run.executed,
            calculation_rules=CALCULATION_RULES,
        )

    def changed_since(self, since: str) -> list[dict[str, Any]]:
        """Cost records, payments and allocations added or changed after ``since``."""
        self._require_company()
        rows = self.store.run(
            self.company_id,
            q.MF_CHANGED_SINCE,
            {"kinds": [*COST_KIND_VALUES, "payment"], "since": since},
        )
        return [
            {
                "change": r["change"],
                "kind": r["kind"],
                "ref": _ref(r["record"]),
                "description": r["record"].get("name"),
                "amount_paise": r["amount_paise"]
                if r["change"] == "allocation"
                else r["record"].get("amount_paise"),
                "status": r["record"].get("status"),
                "target_id": r["target_id"],
            }
            for r in sorted(
                rows, key=lambda r: (r["change"], _ref(r["record"]), r["target_id"] or "")
            )
        ]


def _short(entity_id: str) -> str:
    return entity_id.split(":", 1)[1].upper() if ":" in entity_id else entity_id
