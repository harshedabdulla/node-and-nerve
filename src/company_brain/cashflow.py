"""Cashflow Radar: evidence-backed receivables and commitments investigation.

Answers one question deterministically, without an LLM:

    Which customers owe us money, and which supplier payments or commitments due
    soon have a documented dependency on those collections?

FalkorDB does the relationship work: it computes what each invoice still owes from
the payments allocated to it, excludes superseded commitments, and traverses the
recorded chain invoice <- DEPENDS_ON - commitment -> supplier, plus the purchase
order -> waiting sales order -> customer chain behind the commitment.

Rules this module keeps:
  * A commitment is linked to a collection only through a recorded DEPENDS_ON edge.
    Amounts, dates or shared customers never create a link.
  * A link is "documented" only when an active source record asserts it; otherwise it
    is reported as recorded without a source.
  * Unknowns are reported as open questions or missing information, never as findings.
  * Nothing is sent or paid. Follow-up messages are drafts for a person to review.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from company_brain import queries as q
from company_brain.graph import CompanyNotFound, GraphStore, Row
from company_brain.investigate import _edge, _Run
from company_brain.models import EvidencePath, PathEdge, SourceRef

DEFAULT_QUESTION = (
    "Which customers owe us money, and which supplier payments or commitments due this "
    "week have a documented dependency on those collections?"
)
RESULT_LIMIT = 200

Certainty = Literal["documented", "recorded_without_source"]


class CashflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(default=DEFAULT_QUESTION, min_length=3, max_length=1000)
    as_of: date | None = Field(
        default=None, description="Date used for overdue and due-soon checks. Defaults to today."
    )
    horizon_days: int = Field(
        default=7, ge=1, le=31, description="Commitments due up to this many days after as_of."
    )


class Allocation(BaseModel):
    payment_id: str
    ref: str
    status: str | None
    amount_paise: int | None
    received_on: str | None = None


class Receivable(BaseModel):
    invoice_id: str
    ref: str
    customer_id: str | None
    customer_name: str | None
    status: str | None
    issue_date: str | None
    due_date: str | None
    expected_payment_date: str | None
    amount_paise: int | None
    cleared_paise: int = Field(description="Sum of cleared payments allocated to the invoice.")
    pending_paise: int = Field(description="Allocated payments that have not cleared.")
    outstanding_paise: int | None
    overdue: bool
    days_overdue: int | None
    allocations: list[Allocation]
    orders: list[str]
    source_ids: list[str]
    dependent_commitments: list[str] = Field(
        description="Commitments recorded as depending on collecting this invoice."
    )


class Dependency(BaseModel):
    commitment_id: str
    invoice_id: str
    certainty: Certainty
    source_id: str | None
    note: str | None
    path_id: str


class Commitment(BaseModel):
    commitment_id: str
    ref: str
    name: str | None
    commitment_type: str | None
    status: str | None
    amount_paise: int | None
    due_date: str | None
    past_due: bool
    owed_to: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    depends_on: list[str]
    supersedes: list[str]
    source_ids: list[str]


class CashflowFinding(BaseModel):
    id: str
    category: Literal[
        "overdue_receivable",
        "outstanding_receivable",
        "pending_payment",
        "collection_dependency",
        "linked_order",
        "commitment_due",
        "superseded_commitment",
    ]
    statement: str = Field(description="Deterministic statement rendered from graph facts.")
    entity_ids: list[str]
    path_ids: list[str] = []
    source_ids: list[str] = []
    certainty: Certainty = Field(
        description="documented: an active source record backs it. recorded_without_source: "
        "the fact is in the records but no active source supports it."
    )


class FollowUpDraft(BaseModel):
    invoice_id: str
    invoice_ref: str
    customer_id: str | None
    customer_name: str | None
    message: str
    review_notes: list[str]
    sent: Literal[False] = Field(
        default=False, description="Always false. Cashflow Radar never sends messages."
    )


class CashflowSummary(BaseModel):
    receivables: int
    outstanding_paise: int
    overdue_receivables: int
    overdue_paise: int
    pending_paise: int
    commitments_due: int
    commitments_due_paise: int
    documented_dependencies: int
    dependencies_without_source: int


class CashflowResponse(BaseModel):
    investigation_id: str
    company_id: str
    question: str
    as_of: date
    window_end: date
    summary: CashflowSummary
    findings: list[CashflowFinding]
    receivables: list[Receivable]
    commitments: list[Commitment]
    dependencies: list[Dependency]
    evidence_paths: list[EvidencePath]
    sources: list[SourceRef]
    open_questions: list[str] = Field(
        description="Things the records do not establish either way. Not findings."
    )
    missing_information: list[str]
    warnings: list[str]
    next_steps: list[str]
    follow_up_drafts: list[FollowUpDraft]
    queries_executed: list[str]


# ---------------------------------------------------------------- formatting


def inr(paise: int | None) -> str:
    """Indian-grouped rupees: 30000000 paise -> '₹3,00,000'."""
    if paise is None:
        return "an unrecorded amount"
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join([*groups, tail]) if groups else tail
    return ("-" if paise < 0 else "") + "₹" + text + (f".{rest:02d}" if rest else "")


def day(value: str | None) -> str:
    if not value:
        return "an unrecorded date"
    d = date.fromisoformat(value)
    return f"{d.day} {d:%b %Y}"


def _ref(props: dict[str, Any]) -> str:
    return str(props.get("source_record_id") or props["id"])


def _src(s: SourceRef) -> str:
    """Source as an owner would recognise it: its record id and title."""
    return f"{s.source_record_id or s.id} '{s.name}'"


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("ref") or node["id"])


@dataclass
class _CashflowRun(_Run):
    """The shared investigation accumulator, collecting cashflow findings."""

    cf_findings: list[CashflowFinding] = field(default_factory=list)

    def add_finding(self, **kwargs: Any) -> None:
        self.cf_findings.append(
            CashflowFinding(id=f"finding-{len(self.cf_findings) + 1}", **kwargs)
        )


CertaintyOf = Callable[..., Certainty]


# ---------------------------------------------------------------- investigation


class CashflowInvestigator:
    def __init__(self, store: GraphStore, company_id: str) -> None:
        self.store = store
        self.company_id = company_id

    def investigate(self, request: CashflowRequest) -> CashflowResponse:
        if not self.store.company_exists(self.company_id):
            raise CompanyNotFound(self.company_id)
        as_of = request.as_of or date.today()
        until = as_of + timedelta(days=request.horizon_days)
        run = _CashflowRun(self.store, self.company_id)
        open_questions: list[str] = []
        next_steps: list[str] = []

        # 1. Receivables: the graph sums cleared allocations per invoice.
        rec_rows = run.run(
            q.CF_RECEIVABLES,
            {
                "closed": q.CLOSED_INVOICE_STATUSES,
                "as_of": as_of.isoformat(),
                "limit": RESULT_LIMIT,
            },
        )
        if len(rec_rows) == RESULT_LIMIT:
            run.warnings.append(f"Only the first {RESULT_LIMIT} open invoices were examined.")
        receivables = [self._receivable(row, as_of) for row in rec_rows]
        by_invoice = {r.invoice_id: r for r in receivables}

        # 2. Open, current commitments due by the end of the window.
        com_rows = run.run(
            q.CF_COMMITMENTS_DUE, {"until": until.isoformat(), "limit": RESULT_LIMIT}
        )
        if len(com_rows) == RESULT_LIMIT:
            run.warnings.append(f"Only the first {RESULT_LIMIT} commitments were examined.")
        commitments = [self._commitment(row, as_of) for row in com_rows]

        # 3. Recorded dependencies from outstanding invoices to commitments.
        dep_rows = (
            run.run(q.CF_DEPENDENCY_PATHS, {"invoice_ids": sorted(by_invoice)})
            if by_invoice
            else []
        )
        dep_rows = [
            dr
            for dr in dep_rows
            if not dr["superseded_by"] and (dr["commitment"].get("status") or "open") == "open"
        ]

        # 4. Sources: what supports, mentions or contradicts each record, plus the
        #    sources cited on dependency and supersession edges.
        record_ids = sorted(
            set(by_invoice)
            | {a.payment_id for r in receivables for a in r.allocations}
            | {c.commitment_id for c in commitments}
            | {dr["commitment_id"] for dr in dep_rows}
        )
        sources = [
            SourceRef(relation=row["relation"], about=row["entity_id"], **row["source"])
            for row in (run.run(q.SOURCES_FOR, {"ids": record_ids}) if record_ids else [])
        ]
        edge_cites: dict[str, list[str]] = defaultdict(list)  # source id -> edge descriptions
        for dr in dep_rows:
            if dr["dependency_source_id"]:
                edge_cites[dr["dependency_source_id"]].append(
                    f"{dr['commitment_id']} -[DEPENDS_ON]-> {dr['invoice_id']}"
                )
        for c in commitments:
            row = self._row_of(com_rows, c)
            for link in row["supersedes"] + row["depends_on"]:
                target = link.get("id") or link.get("invoice_id")
                rel = "SUPERSEDES" if "id" in link else "DEPENDS_ON"
                if link["source_id"]:
                    edge_cites[link["source_id"]].append(f"{c.commitment_id} -[{rel}]-> {target}")
        edge_sources: dict[str, SourceRef] = {}
        if edge_cites:
            for row in run.run(q.CF_SOURCES_BY_ID, {"ids": sorted(edge_cites)}):
                for about in dict.fromkeys(edge_cites[row["source"]["id"]]):
                    ref = SourceRef(relation="asserts", about=about, **row["source"])
                    sources.append(ref)
                    edge_sources[ref.id] = ref
        sources.sort(key=lambda s: (s.about, s.relation, s.id))
        refs = {r.invoice_id: r.ref for r in receivables}
        refs |= {a.payment_id: a.ref for r in receivables for a in r.allocations}
        refs |= {c.commitment_id: c.ref for c in commitments}
        refs |= {dr["commitment_id"]: _ref(dr["commitment"]) for dr in dep_rows}
        support, contradictions = self._support(run, sources, refs)

        def certainty(*ids: str) -> Certainty:
            return "documented" if any(support.get(i) for i in ids) else "recorded_without_source"

        # 5. Findings about receivables.
        for r in sorted(
            receivables, key=lambda r: (not r.overdue, -(r.days_overdue or 0), r.invoice_id)
        ):
            self._receivable_findings(run, r, support, contradictions, certainty, open_questions)

        # 6. Dependencies: one evidence path per recorded link.
        dependencies: list[Dependency] = []
        seen: set[tuple[str, str]] = set()
        for row in dep_rows:
            key = (row["invoice_id"], row["commitment_id"])
            dependency = self._dependency_finding(
                run, row, by_invoice[row["invoice_id"]], edge_sources, until, key in seen
            )
            if dependency and key not in seen:
                dependencies.append(dependency)
                by_invoice[row["invoice_id"]].dependent_commitments.append(row["commitment_id"])
            seen.add(key)

        for r in receivables:
            if r.overdue and not r.dependent_commitments:
                open_questions.append(
                    f"No commitment is recorded as depending on {r.ref} from {r.customer_name}. "
                    "The records do not show whether any payment relies on this collection."
                )

        # 7. Commitments due in the window.
        for c in commitments:
            self._commitment_findings(
                run, c, com_rows, by_invoice, support, certainty, open_questions
            )

        # 8. Practical next steps and drafts (never sent).
        drafts = self._next_steps(
            receivables, dependencies, commitments, contradictions, until, next_steps
        )
        missing = list(dict.fromkeys(run.missing))
        summary = CashflowSummary(
            receivables=len(receivables),
            outstanding_paise=sum(r.outstanding_paise or 0 for r in receivables),
            overdue_receivables=sum(r.overdue for r in receivables),
            overdue_paise=sum(r.outstanding_paise or 0 for r in receivables if r.overdue),
            pending_paise=sum(r.pending_paise for r in receivables),
            commitments_due=len(commitments),
            commitments_due_paise=sum(c.amount_paise or 0 for c in commitments),
            documented_dependencies=sum(d.certainty == "documented" for d in dependencies),
            dependencies_without_source=sum(d.certainty != "documented" for d in dependencies),
        )
        return CashflowResponse(
            investigation_id=f"cfr_{uuid.uuid4().hex[:16]}",
            company_id=self.company_id,
            question=request.question,
            as_of=as_of,
            window_end=until,
            summary=summary,
            findings=run.cf_findings,
            receivables=receivables,
            commitments=commitments,
            dependencies=dependencies,
            evidence_paths=run.paths,
            sources=sources,
            open_questions=list(dict.fromkeys(open_questions)),
            missing_information=missing,
            warnings=list(dict.fromkeys(run.warnings)),
            next_steps=list(dict.fromkeys(next_steps)),
            follow_up_drafts=drafts,
            queries_executed=run.executed,
        )

    # ------------------------------------------------------------ rows -> views

    def _receivable(self, row: Row, as_of: date) -> Receivable:
        inv = row["invoice"]
        customer = row["customers"][0] if row["customers"] else {}
        allocations = [
            Allocation(
                payment_id=a["id"],
                ref=a.get("ref") or a["id"],
                status=a.get("status"),
                amount_paise=a.get("amount_paise"),
                received_on=a.get("received_on"),
            )
            for a in sorted(row["allocations"], key=lambda a: a["id"])
        ]
        due = inv.get("due_date")
        return Receivable(
            invoice_id=inv["id"],
            ref=_ref(inv),
            customer_id=customer.get("id"),
            customer_name=customer.get("name"),
            status=inv.get("status"),
            issue_date=inv.get("issue_date"),
            due_date=due,
            expected_payment_date=inv.get("expected_payment_date"),
            amount_paise=inv.get("amount_paise"),
            cleared_paise=int(row["cleared_paise"] or 0),
            pending_paise=sum(a.amount_paise or 0 for a in allocations if a.status == "pending"),
            outstanding_paise=row["outstanding_paise"],
            overdue=bool(row["overdue"]),
            days_overdue=(as_of - date.fromisoformat(due)).days if row["overdue"] and due else None,
            allocations=allocations,
            orders=sorted(o["id"] for o in row["orders"]),
            source_ids=[],
            dependent_commitments=[],
        )

    def _commitment(self, row: Row, as_of: date) -> Commitment:
        k = row["commitment"]
        due = k.get("due_date")
        return Commitment(
            commitment_id=k["id"],
            ref=_ref(k),
            name=k.get("name"),
            commitment_type=k.get("commitment_type"),
            status=k.get("status"),
            amount_paise=k.get("amount_paise"),
            due_date=due,
            past_due=bool(due and due < as_of.isoformat()),
            owed_to=sorted(row["owed_to"], key=lambda x: x["id"]),
            orders=sorted(row["orders"], key=lambda x: x["id"]),
            depends_on=sorted(d["invoice_id"] for d in row["depends_on"]),
            supersedes=sorted(s["id"] for s in row["supersedes"]),
            source_ids=[],
        )

    @staticmethod
    def _row_of(com_rows: list[Row], c: Commitment) -> Row:
        return next(row for row in com_rows if row["commitment"]["id"] == c.commitment_id)

    def _support(
        self, run: _CashflowRun, sources: list[SourceRef], refs: dict[str, str]
    ) -> tuple[dict[str, list[str]], dict[str, list[SourceRef]]]:
        support: dict[str, list[str]] = defaultdict(list)
        contradictions: dict[str, list[SourceRef]] = defaultdict(list)
        for s in sources:
            if s.status != "active":
                run.warnings.append(
                    f"{_src(s)} was {s.status}; it is not counted as evidence for "
                    f"{refs.get(s.about, s.about)}."
                )
            elif s.relation == "supports":
                support[s.about].append(s.id)
            elif s.relation == "contradicts":
                contradictions[s.about].append(s)
                run.warnings.append(
                    f"Conflicting record: {_src(s)} disputes {refs.get(s.about, s.about)}"
                    + (f': "{s.excerpt}"' if s.excerpt else "")
                )
        return support, contradictions

    # ------------------------------------------------------------ findings

    def _receivable_findings(
        self,
        run: _CashflowRun,
        r: Receivable,
        support: dict[str, list[str]],
        contradictions: dict[str, list[SourceRef]],
        certainty: CertaintyOf,
        open_questions: list[str],
    ) -> None:
        r.source_ids = list(support.get(r.invoice_id, []))
        who = r.customer_name or "An unrecorded customer"
        edges = []
        if r.customer_id:
            edges.append(PathEdge(type="BILLED_TO", from_id=r.invoice_id, to_id=r.customer_id))
        edges += [
            PathEdge(type="ALLOCATED_TO", from_id=a.payment_id, to_id=r.invoice_id)
            for a in r.allocations
        ]
        path_ids = [run.add_path(f"{r.ref}: customer and payments", edges)] if edges else []
        paid = f" after {inr(r.cleared_paise)} in cleared payments" if r.cleared_paise else ""
        if r.outstanding_paise is None:
            run.missing.append(f"{r.ref} has no invoice amount recorded; its balance is unknown.")
        if r.overdue:
            statement = (
                f"{who} owes {inr(r.outstanding_paise)} on invoice {r.ref} "
                f"({inr(r.amount_paise)} billed{paid}). It was due {day(r.due_date)} and is "
                f"{r.days_overdue} days overdue."
            )
            category: Literal["overdue_receivable", "outstanding_receivable"] = "overdue_receivable"
        else:
            when = (
                f"due {day(r.due_date)}"
                if r.due_date
                else "with no due date recorded, so whether it is overdue is unknown"
            )
            statement = (
                f"{who} owes {inr(r.outstanding_paise)} on invoice {r.ref} "
                f"({inr(r.amount_paise)} billed{paid}), {when}."
            )
            category = "outstanding_receivable"
        if r.expected_payment_date:
            statement += (
                f" The customer is recorded as expecting to pay by {day(r.expected_payment_date)}."
            )
        run.add_finding(
            category=category,
            statement=statement,
            entity_ids=[r.invoice_id, *([r.customer_id] if r.customer_id else [])],
            path_ids=path_ids,
            source_ids=r.source_ids,
            certainty=certainty(r.invoice_id),
        )
        if not r.due_date:
            run.missing.append(
                f"{r.ref} has no due date, so it cannot be checked for being overdue."
            )
        if not r.customer_id:
            run.missing.append(f"{r.ref} is not linked to a customer.")
        if not r.source_ids:
            run.missing.append(f"No source document supports invoice {r.ref}.")
        if r.status == "paid":
            run.warnings.append(
                f"{r.ref} is marked paid, but cleared payments recorded against it total "
                f"{inr(r.cleared_paise)} of {inr(r.amount_paise)}."
            )
        for a in r.allocations:
            if a.status == "pending":
                run.add_finding(
                    category="pending_payment",
                    statement=f"Payment {a.ref} of "
                    f"{inr(a.amount_paise)} from {who} is allocated to {r.ref} but has not "
                    "cleared, so it is not counted as received.",
                    entity_ids=[a.payment_id, r.invoice_id],
                    path_ids=path_ids,
                    source_ids=support.get(a.payment_id, []),
                    certainty=certainty(a.payment_id),
                )
                open_questions.append(
                    f"Whether payment {a.ref} for {r.ref} will clear is not yet recorded."
                )
            elif a.status == "bounced":
                run.warnings.append(f"Payment {a.ref} allocated to {r.ref} is recorded as bounced.")

    def _dependency_finding(
        self,
        run: _CashflowRun,
        row: Row,
        r: Receivable,
        edge_sources: dict[str, SourceRef],
        until: date,
        repeat: bool,
    ) -> Dependency | None:
        k = row["commitment"]
        kid = k["id"]
        edges = [_edge(e) for e in row["dependency_rels"] + row["owed_rels"]]
        order_edges = [_edge(e) for e in row["order_rels"]]
        path_id = run.add_path(
            f"collection of {r.ref} -> {_ref(k)}"
            + (" -> purchase and sales orders" if order_edges else ""),
            edges + order_edges,
        )
        if repeat:
            return None
        sid = row["dependency_source_id"]
        documented = bool(sid and sid in edge_sources and edge_sources[sid].status == "active")
        owed = [n for n in row["owed_nodes"] if n["id"] != kid]
        to = ", ".join(_label(n) for n in owed) or "an unrecorded party"
        due = k.get("due_date")
        later = ", after this window" if due and due > until.isoformat() else ""
        overdue = f", {r.days_overdue} days overdue" if r.overdue else ""
        statement = (
            f"{_ref(k)} '{k.get('name')}' ({inr(k.get('amount_paise'))} to {to}, due "
            f"{day(due)}{later}) is recorded as depending on collecting {r.ref} from "
            f"{r.customer_name} ({inr(r.outstanding_paise)} outstanding{overdue})."
        )
        if documented:
            statement += f" Documented by {_src(edge_sources[_must(sid)])}."
        else:
            statement += " No source document supports this link."
            run.missing.append(
                f"No source document supports the recorded link between {_ref(k)} and {r.ref}; "
                "confirm it before relying on it."
            )
        run.add_finding(
            category="collection_dependency",
            statement=statement,
            entity_ids=[kid, r.invoice_id, *(n["id"] for n in owed)],
            path_ids=[path_id],
            source_ids=[sid] if documented and sid else [],
            certainty="documented" if documented else "recorded_without_source",
        )
        nodes = {n["id"]: n for n in row["order_nodes"]}
        so = next((n for n in nodes.values() if n["kind"] == "salesorder"), None)
        po = next((n for n in nodes.values() if n["kind"] == "purchaseorder"), None)
        buyer = next((n for n in nodes.values() if n["kind"] == "customer"), None)
        if so and po and buyer:
            same = (
                " (the same customer that owes " + r.ref + ")"
                if buyer["id"] == r.customer_id
                else ""
            )
            run.add_finding(
                category="linked_order",
                statement=f"{_ref(k)} pays for {po.get('ref') or po['id']} "
                f"(status: {po.get('status')}), which supplies {so.get('ref') or so['id']} "
                f"for {_label(buyer)}{same}; status: {so.get('status')}, required by "
                f"{day(so.get('required_by'))}.",
                entity_ids=[kid, po["id"], so["id"], buyer["id"]],
                path_ids=[path_id],
                source_ids=[e.source_id for e in order_edges if e.source_id][:1],
                certainty="documented"
                if any(e.source_id for e in order_edges)
                else "recorded_without_source",
            )
        return Dependency(
            commitment_id=kid,
            invoice_id=r.invoice_id,
            certainty="documented" if documented else "recorded_without_source",
            source_id=sid,
            note=row.get("note"),
            path_id=path_id,
        )

    def _commitment_findings(
        self,
        run: _CashflowRun,
        c: Commitment,
        com_rows: list[Row],
        by_invoice: dict[str, Receivable],
        support: dict[str, list[str]],
        certainty: CertaintyOf,
        open_questions: list[str],
    ) -> None:
        c.source_ids = list(support.get(c.commitment_id, []))
        to = ", ".join(_label(x) for x in c.owed_to) or "an unrecorded party"
        when = (
            f"was due {day(c.due_date)} and is still open"
            if c.past_due
            else f"is due {day(c.due_date)}"
        )
        edges = [
            PathEdge(type="OWED_TO", from_id=c.commitment_id, to_id=x["id"]) for x in c.owed_to
        ]
        edges += [
            PathEdge(type="FOR_ORDER", from_id=c.commitment_id, to_id=o["id"]) for o in c.orders
        ]
        path_ids = [run.add_path(f"{c.ref}: who is owed and which order", edges)] if edges else []
        kind = (c.commitment_type or "commitment").replace("_", " ")
        owed = f"for {to}" if c.amount_paise is None else f"{inr(c.amount_paise)} to {to}"
        orders = "; ".join(f"{_label(o)} (status: {o.get('status')})" for o in c.orders)
        run.add_finding(
            category="commitment_due",
            statement=f"{c.ref} '{c.name}' ({kind}, {owed}) {when}."
            + (f" Linked order: {orders}." if orders else ""),
            entity_ids=[
                c.commitment_id,
                *(x["id"] for x in c.owed_to),
                *(o["id"] for o in c.orders),
            ],
            path_ids=path_ids,
            source_ids=c.source_ids,
            certainty=certainty(c.commitment_id),
        )
        if not c.due_date:
            run.missing.append(f"{c.ref} has no due date recorded.")
        if c.amount_paise is None and c.commitment_type == "supplier_payment":
            run.missing.append(f"{c.ref} has no amount recorded.")
        if not c.owed_to:
            run.missing.append(f"{c.ref} does not record who it is owed to.")
        if not c.source_ids:
            run.missing.append(f"No source document supports commitment {c.ref}.")
        if c.commitment_type == "supplier_payment":
            if not c.depends_on:
                open_questions.append(
                    f"No dependency on a customer collection is recorded for {c.ref} ({to}, "
                    f"{inr(c.amount_paise)}). Whether it relies on incoming payments is not "
                    "recorded."
                )
            for dep in self._row_of(com_rows, c)["depends_on"]:
                if dep["invoice_id"] not in by_invoice:
                    edge = PathEdge(
                        type="DEPENDS_ON",
                        from_id=c.commitment_id,
                        to_id=dep["invoice_id"],
                        source_id=dep["source_id"],
                    )
                    run.add_finding(
                        category="collection_dependency",
                        statement=f"{c.ref} is recorded as depending on collecting "
                        f"{dep['ref'] or dep['invoice_id']}, which has no outstanding balance "
                        "in the records.",
                        entity_ids=[c.commitment_id, dep["invoice_id"]],
                        path_ids=[run.add_path(f"{c.ref} depends on a settled invoice", [edge])],
                        source_ids=[dep["source_id"]] if dep["source_id"] else [],
                        certainty="documented" if dep["source_id"] else "recorded_without_source",
                    )
        for old in self._row_of(com_rows, c)["supersedes"]:
            edge = PathEdge(
                type="SUPERSEDES",
                from_id=c.commitment_id,
                to_id=old["id"],
                source_id=old["source_id"],
            )
            changes = []
            if old.get("due_date") != c.due_date:
                changes.append(f"due date {day(old.get('due_date'))} -> {day(c.due_date)}")
            if old.get("amount_paise") != c.amount_paise:
                changes.append(f"amount {inr(old.get('amount_paise'))} -> {inr(c.amount_paise)}")
            run.add_finding(
                category="superseded_commitment",
                statement=f"{c.ref} replaces {old['ref'] or old['id']}"
                + (f" ({'; '.join(changes)})" if changes else "")
                + ". The earlier version is kept as history and excluded from what is due.",
                entity_ids=[c.commitment_id, old["id"]],
                path_ids=[run.add_path(f"{c.ref} supersedes {old['ref'] or old['id']}", [edge])],
                source_ids=[old["source_id"]] if old.get("source_id") else [],
                certainty="documented" if old.get("source_id") else "recorded_without_source",
            )

    def _next_steps(
        self,
        receivables: list[Receivable],
        dependencies: list[Dependency],
        commitments: list[Commitment],
        contradictions: dict[str, list[SourceRef]],
        until: date,
        steps: list[str],
    ) -> list[FollowUpDraft]:
        due = {c.commitment_id: c for c in commitments}
        by_invoice = {r.invoice_id: r for r in receivables}
        for d in dependencies:
            r = by_invoice[d.invoice_id]
            c = due.get(d.commitment_id)
            if c is None:
                continue
            if d.certainty == "documented":
                steps.append(
                    f"Follow up with {r.customer_name} about {r.ref} ({inr(r.outstanding_paise)} "
                    f"outstanding) before {day(c.due_date)}: {c.ref} to "
                    f"{', '.join(_label(x) for x in c.owed_to)} ({inr(c.amount_paise)}) is "
                    "documented as depending on it."
                )
            else:
                steps.append(
                    f"Confirm whether {c.ref} really depends on {r.ref}, and record the document "
                    "that says so."
                )
        for r in receivables:
            for s in contradictions.get(r.invoice_id, []):
                steps.append(
                    f"Review {_src(s)} disputing {r.ref} before sending a payment reminder."
                )
            if r.pending_paise:
                steps.append(
                    f"Check whether the pending payment for {r.ref} has cleared before "
                    "following up."
                )
            if not r.due_date:
                steps.append(f"Record a due date for {r.ref}.")
        for r in receivables:
            if r.overdue and not r.dependent_commitments and not r.pending_paise:
                steps.append(
                    f"Follow up with {r.customer_name} on {r.ref} ({inr(r.outstanding_paise)}, "
                    f"{r.days_overdue} days overdue)."
                )
        drafts = []
        for r in sorted(receivables, key=lambda r: (-(r.days_overdue or 0), r.invoice_id)):
            if not r.overdue:
                continue
            notes = [
                f"{_src(s)} disputes this invoice: review it first."
                for s in contradictions.get(r.invoice_id, [])
            ]
            if r.pending_paise:
                notes.append(
                    f"A payment of {inr(r.pending_paise)} is pending; check whether it has cleared."
                )
            promise = (
                f" You had mentioned payment by {day(r.expected_payment_date)}."
                if r.expected_payment_date
                else ""
            )
            drafts.append(
                FollowUpDraft(
                    invoice_id=r.invoice_id,
                    invoice_ref=r.ref,
                    customer_id=r.customer_id,
                    customer_name=r.customer_name,
                    message=(
                        f"Namaste {r.customer_name or ''},\n\n"
                        f"This is a reminder that invoice {r.ref}"
                        + (f" dated {day(r.issue_date)}" if r.issue_date else "")
                        + f" has an outstanding balance of {inr(r.outstanding_paise)}, "
                        f"which was due on {day(r.due_date)}.{promise} Could you please confirm "
                        "when we can expect the payment?\n\nThank you,\n[Your name]"
                    ),
                    review_notes=notes,
                )
            )
        return drafts


def _must(value: str | None) -> str:
    assert value is not None
    return value
