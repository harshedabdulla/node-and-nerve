"""Command-line entry point: seed, investigate, evaluate and run the demos.

``company-brain cashflow ...`` holds the Cashflow Radar commands.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from company_brain.cashflow import (
    CashflowInvestigator,
    CashflowRequest,
    CashflowResponse,
    day,
    inr,
)
from company_brain.config import get_settings
from company_brain.contract_cost import (
    ContractCostInvestigator,
    ContractCostRequest,
    ContractCostResponse,
    OverviewResponse,
    UnallocatedResponse,
)
from company_brain.csv_import import FILES, CsvImporter, ImportReport
from company_brain.evaluation import run_evaluation
from company_brain.graph import FalkorDBStore, GraphError, GraphStore
from company_brain.ingest import Ingestor
from company_brain.investigate import Investigator
from company_brain.models import (
    Event,
    InvestigationRequest,
    InvestigationResponse,
    OwnershipChanged,
    SourceWithdrawn,
    TicketStatusChanged,
)
from company_brain.seed import (
    CASHFLOW_COMPANY,
    CASHFLOW_UPDATES_OBSERVED_AT,
    CONTRACTS_COMPANY,
    load_dataset,
    seed,
    seed_cashflow,
    seed_contracts,
)

DEMO_QUESTION = (
    "Why is Project Atlas behind schedule, who owns the blockers, which previous "
    "decisions are relevant, and what evidence supports the findings?"
)
DEMO_AS_OF = date(2026, 10, 16)
CASHFLOW_AS_OF = date(2026, 10, 16)


def _store() -> GraphStore:
    return FalkorDBStore(get_settings())


def _seed(store: GraphStore, company: str, reset: bool) -> None:
    if reset:
        store.drop_company(company)
    summary = seed(store, load_dataset(), company)
    print(
        f"seeded {summary.company_id}: {summary.entities} entities, {summary.sources} sources, "
        f"{summary.relationships} relationships (idempotent; safe to re-run)"
    )


def print_investigation(r: InvestigationResponse) -> None:
    print(f"\nQ: {r.question}\n   company={r.company_id} as_of={r.as_of} id={r.investigation_id}")
    print("\nMatched: " + ", ".join(f"{e.id} ({e.match_reason})" for e in r.matched_entities))
    print("\nFindings")
    for f in r.findings:
        mark = "+" if f.supported else "?"
        refs = " ".join(f.path_ids + f.source_ids)
        print(f"  [{mark}] {f.category:<19} {f.statement}")
        if refs:
            print(f"      {'':<19} evidence: {refs}")
    print("\nEvidence paths")
    for p in r.evidence_paths:
        print(f"  {p.id}: {p.description}")
        for e in p.edges:
            via = f"  (asserted by {e.source_id})" if e.source_id else ""
            print(f"      {e.from_id} -[{e.type}]-> {e.to_id}{via}")
    print("\nSources")
    for s in r.sources:
        print(f"  {s.relation:<11} {s.about:<22} {s.id} [{s.status}] {s.uri or ''}")
    print("\nCurrent owners")
    for o in r.current_owners:
        print(f"  {o.entity_id:<26} {o.owner_id} ({o.owner_name})")
    print("\nExperts (from recorded relationships)")
    for x in r.experts:
        print(f"  {x.score}  {x.person_id}: " + ", ".join(b["basis"] for b in x.basis))
    print("\nDecisions")
    for d in r.decisions:
        state = "CURRENT   " if d.current else "superseded"
        print(f"  {state} {d.id} {d.name}" + (f"  <- {d.superseded_by}" if d.superseded_by else ""))
    print("\nMissing information")
    for m in r.missing_information or ["(none)"]:
        print(f"  - {m}")
    print("\nWarnings")
    for w in r.warnings or ["(none)"]:
        print(f"  ! {w}")
    print(f"\nGraph queries executed: {', '.join(r.queries_executed)}")


def cmd_investigate(args: argparse.Namespace) -> None:
    response = Investigator(_store(), args.company).investigate(
        InvestigationRequest(
            question=args.question,
            focus_ids=args.focus or [],
            as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        )
    )
    if args.json:
        print(response.model_dump_json(indent=2, by_alias=True))
    else:
        print_investigation(response)


def cmd_eval(args: argparse.Namespace) -> None:
    store = _store()
    _seed(store, args.company, reset=True)
    results = run_evaluation(store, args.company, load_dataset())
    rows: list[dict[str, Any]] = []
    for graph, base in results:
        rows += [graph.as_dict(), base.as_dict()]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    cols = [
        ("entity_recall", "entities"),
        ("path_found", "path"),
        ("source_recall", "sources"),
        ("superseded_excluded", "superseded excl."),
        ("missing_evidence_flagged", "missing flagged"),
        ("leaked_entities", "leaked"),
    ]
    print(f"\n{'scenario':<27}{'system':<10}" + "".join(f"{h:<18}" for _, h in cols))
    for row in rows:
        cells = [str(len(row[k])) if isinstance(row[k], list) else str(row[k]) for k, _ in cols]
        print(f"{row['scenario']:<27}{row['system']:<10}" + "".join(f"{c:<18}" for c in cells))
    for row in rows:
        if row["missing_entities"] or row["missing_sources"]:
            print(
                f"\n{row['system']}/{row['scenario']} missed entities={row['missing_entities']} "
                f"sources={row['missing_sources']}"
            )


def _summary(r: InvestigationResponse) -> dict[str, Any]:
    return {
        "owners": sorted(f"{o.entity_id}={o.owner_id}" for o in r.current_owners),
        "findings": [f.statement for f in r.findings],
        "chains": sorted(p.description for p in r.evidence_paths if "chain" in p.description),
        "missing": r.missing_information,
        "warnings": r.warnings,
    }


def cmd_demo_update(args: argparse.Namespace) -> None:
    """Show that recorded changes alter the graph's answer."""
    store = _store()
    _seed(store, args.company, reset=True)
    investigator = Investigator(store, args.company)
    request = InvestigationRequest(question=DEMO_QUESTION, as_of=DEMO_AS_OF)
    before = _summary(investigator.investigate(request))
    ingest = Ingestor(store, args.company)
    events: list[Event] = [
        OwnershipChanged(
            type="ownership_changed",
            event_id="evt-demo-001",
            entity_id="ticket:atlas-142",
            new_owner_id="person:lena-ortiz",
            occurred_at=datetime(2026, 10, 16, 10, 0, tzinfo=UTC),
        ),
        SourceWithdrawn(
            type="source_withdrawn",
            event_id="evt-demo-002",
            source_id="source:postmortem-009",
            reason="draft replaced by revised postmortem",
            occurred_at=datetime(2026, 10, 16, 10, 5, tzinfo=UTC),
        ),
        TicketStatusChanged(
            type="ticket_status_changed",
            event_id="evt-demo-003",
            ticket_id="ticket:int-88",
            status="done",
            occurred_at=datetime(2026, 10, 16, 11, 0, tzinfo=UTC),
        ),
    ]
    for event in events:
        print(f"\n=== event {event.event_id} ({event.type})")
        for change in ingest.apply_event(event).changes:
            print(f"    {change}")
        ingest.apply_event(event)  # replaying the same event is a no-op
        after = _summary(investigator.investigate(request))
        _print_diff(before, after)
        before = after


def _print_diff(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in before:
        removed = [x for x in before[key] if x not in after[key]]
        added = [x for x in after[key] if x not in before[key]]
        if removed or added:
            print(f"  {key}:")
            for x in removed:
                print(f"    - {x}")
            for x in added:
                print(f"    + {x}")


# ---------------------------------------------------------------- Cashflow Radar


def print_import(report: ImportReport) -> None:
    state = "valid" if report.valid else "INVALID"
    action = (
        "dry run, nothing written"
        if report.dry_run
        else ("written" if report.written else "nothing written")
    )
    rows = ", ".join(f"{name} {n}" for name, n in report.rows.items())
    print(f"import into {report.company_id}: {state}, {action}")
    print(f"  rows: {rows or '(none)'}")
    print(f"  records: {report.entities}, links: {report.relationships}")
    if report.ignored_stale:
        print(f"  kept newer version already stored for: {', '.join(report.ignored_stale)}")
    for issue in report.errors:
        where = f"{issue.file}" + (f" row {issue.row}" if issue.row is not None else "")
        print(
            f"  error   {where}"
            + (f" [{issue.column}]" if issue.column else "")
            + f": {issue.message}"
        )
    for issue in report.warnings:
        where = f"{issue.file}" + (f" row {issue.row}" if issue.row is not None else "")
        print(f"  warning {where}: {issue.message}")


def print_cashflow(r: CashflowResponse) -> None:
    s = r.summary
    print(f"\nCashflow Radar: {r.company_id}, as of {day(r.as_of.isoformat())}")
    print(f"Q: {r.question}")
    print("\nSummary")
    print(
        f"  Customers owe {inr(s.outstanding_paise)} across {s.receivables} invoice(s); "
        f"{inr(s.overdue_paise)} of it is overdue ({s.overdue_receivables} invoice(s))."
    )
    if s.pending_paise:
        print(f"  {inr(s.pending_paise)} in received payments has not cleared yet.")
    print(
        f"  {s.commitments_due} commitment(s) due by {day(r.window_end.isoformat())}, "
        f"totalling {inr(s.commitments_due_paise)}."
    )
    print(
        f"  Recorded links from collections to commitments: {s.documented_dependencies} "
        f"documented, {s.dependencies_without_source} without a source document."
    )
    print("\nNeeds attention")
    for i, step in enumerate(r.next_steps or ["(nothing)"], start=1):
        print(f"  {i}. {step}")
    print("\nFindings   [+] backed by a source document   [?] recorded without one")
    for f in r.findings:
        mark = "+" if f.certainty == "documented" else "?"
        print(f"  [{mark}] {f.category.replace('_', ' ')}: {f.statement}")
        refs = " ".join(f.path_ids + f.source_ids)
        if refs:
            print(f"        evidence: {refs}")
    print("\nNot established by the records")
    for item in r.open_questions or ["(none)"]:
        print(f"  ? {item}")
    print("\nMissing information")
    for item in r.missing_information or ["(none)"]:
        print(f"  - {item}")
    print("\nConflicts and warnings")
    for item in r.warnings or ["(none)"]:
        print(f"  ! {item}")
    print("\nHow the records connect")
    for p in r.evidence_paths:
        print(f"  {p.id}: {p.description}")
        for e in p.edges:
            via = f"  (per {e.source_id})" if e.source_id else ""
            print(f"      {e.from_id} -[{e.type}]-> {e.to_id}{via}")
    print("\nSource records")
    for src in r.sources:
        print(f"  {src.relation:<11} {src.about:<44} {src.id} '{src.name}' [{src.status}]")
    print("\nDraft follow-ups (NOT sent; review and edit before using)")
    for d in r.follow_up_drafts or []:
        print(f"  --- {d.invoice_ref} to {d.customer_name}")
        for line in d.message.splitlines():
            print(f"  | {line}")
        for note in d.review_notes:
            print(f"  review: {note}")
    if not r.follow_up_drafts:
        print("  (none)")
    print(f"\nGraph queries run: {', '.join(r.queries_executed)}")


def _cf_company(args: argparse.Namespace) -> str:
    return str(args.company or CASHFLOW_COMPANY)


def cmd_cf_seed(args: argparse.Namespace) -> None:
    store, company = _store(), _cf_company(args)
    if args.reset:
        store.drop_company(company)
    print_import(seed_cashflow(store, company))


def cmd_cf_import(args: argparse.Namespace) -> int:
    folder = Path(args.directory)
    files = {p.name: p.read_text("utf-8") for p in sorted(folder.glob("*.csv"))}
    if not files:
        print(f"error: no .csv files in {folder}; expected some of {', '.join(FILES)}")
        return 2
    report = CsvImporter(_store(), _cf_company(args)).run(files, dry_run=args.dry_run)
    print_import(report)
    return 0 if report.valid else 2


def _cf_request(args: argparse.Namespace) -> CashflowRequest:
    return CashflowRequest(
        question=args.question,
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        horizon_days=args.days,
    )


def cmd_cf_investigate(args: argparse.Namespace) -> None:
    response = CashflowInvestigator(_store(), _cf_company(args)).investigate(_cf_request(args))
    if args.json:
        print(response.model_dump_json(indent=2, by_alias=True))
    else:
        print_cashflow(response)


def _cf_summary(r: CashflowResponse) -> dict[str, Any]:
    return {
        "findings": [f.statement for f in r.findings],
        "needs attention": r.next_steps,
        "not established": r.open_questions,
        "missing": r.missing_information,
        "warnings": r.warnings,
    }


def cmd_cf_demo(args: argparse.Namespace) -> None:
    """Load, investigate, re-import (no change), then import later records and compare."""
    store, company = _store(), _cf_company(args)
    store.drop_company(company)
    print("=== 1. Import the distributor's records (CSV)")
    print_import(seed_cashflow(store, company))
    investigator = CashflowInvestigator(store, company)
    before = investigator.investigate(CashflowRequest(as_of=CASHFLOW_AS_OF))
    print_cashflow(before)

    print("\n=== 2. Import the same files again: nothing changes")
    print_import(seed_cashflow(store, company))
    again = investigator.investigate(CashflowRequest(as_of=CASHFLOW_AS_OF))
    same = _cf_summary(again) == _cf_summary(before)
    print(f"  investigation unchanged: {same}")

    print("\n=== 3. 20 Oct: Shree Ganesh pays the balance; INV-1004 gets a due date")
    as_of = date(2026, 10, 20)
    on_the_day = _cf_summary(investigator.investigate(CashflowRequest(as_of=as_of)))
    print_import(
        seed_cashflow(
            store, company, dataset="varsha-updates", observed_at=CASHFLOW_UPDATES_OBSERVED_AT
        )
    )
    old = _cf_summary(investigator.investigate(CashflowRequest(as_of=as_of)))
    print(f"  changes in the {as_of} investigation caused by the new records:")
    _print_diff(on_the_day, old)

    print("\n=== 4. An older export arrives late: the newer records are kept")
    print_import(seed_cashflow(store, company))
    after = _cf_summary(investigator.investigate(CashflowRequest(as_of=as_of)))
    print(f"  investigation unchanged by the stale import: {after == old}")


# ---------------------------------------------------------------- contract costing


def _short_id(entity_id: str) -> str:
    return entity_id.split(":", 1)[1].upper() if ":" in entity_id else entity_id


def print_contract(r: ContractCostResponse) -> None:
    c, b, p, acct = r.contract, r.cost_breakdown, r.profitability, r.customer_account
    print(f"\nContract {c.ref} for {c.customer_name} (as of {day(r.as_of.isoformat())})")
    print(
        f"  {c.body_count} {c.body_type} bodies, {inr(c.value_paise)}, contracted "
        f"{day(c.contract_date)}, expected {day(c.expected_completion)}, status {c.status}"
    )
    print("  Jobs: " + ", ".join(f"{j.ref} ({j.status})" for j in c.jobs))
    print("\nRecorded direct cost (provisional)")
    for label, value in [
        ("Materials", b.materials_paise),
        ("Labour", b.labour_paise),
        ("Machinery", b.machinery_paise),
        ("Other direct", b.other_paise),
        ("Total", b.total_paise),
    ]:
        print(f"  {label:<14}{inr(value):>14}")
    excluded = b.excluded_void + b.excluded_without_amount
    print(
        f"  ({b.entries} charged entries"
        + (f"; excluded: {', '.join(excluded)}" if excluded else "")
        + ")"
    )
    print("\nProfitability (provisional, recorded direct costs only)")
    print(f"  Contract value       {inr(p.contract_value_paise):>14}")
    print(f"  Recorded direct cost {inr(p.recorded_direct_cost_paise):>14}")
    print(
        f"  Provisional profit   {inr(p.provisional_profit_paise):>14}  "
        f"({p.provisional_margin_percent}% of contract value)"
    )
    if p.pending_allocation_paise:
        print(
            f"  If all {inr(p.pending_allocation_paise)} of unsplit costs noted for it were "
            f"charged here: {inr(p.profit_if_all_pending_charged_here_paise)}"
        )
    print("\nMoney owed (settles amounts, never adds cost or revenue)")
    print(
        f"  Customer has paid {inr(acct.received_paise)}; still to receive "
        f"{inr(acct.outstanding_paise)}"
        + (
            f"; {inr(acct.pending_receipts_paise)} not cleared"
            if acct.pending_receipts_paise
            else ""
        )
    )
    owed = [x for x in r.supplier_bills if x.outstanding_paise]
    for x in owed:
        shared = (
            f", whole bill shared with {', '.join(map(_short_id, x.shared_with))}"
            if x.shared_with
            else ""
        )
        print(
            f"  Owed to {x.supplier}: {inr(x.outstanding_paise)} on {x.ref} "
            f"(bill {inr(x.bill_amount_paise)}, book says {x.book_paid_status}, "
            f"{x.reconciliation}{shared})"
        )
    for w in r.wages_unpaid:
        print(f"  Wages unpaid: {w['ref']} {w['worker']} {inr(w['amount_paise'])} ({w['period']})")

    def section(title: str, kinds: set[str], extra: list[str] | None = None) -> None:
        items = [f for f in r.findings if f.kind in kinds]
        print(f"\n{title}")
        for f in items:
            refs = " ".join(f.path_ids[:3] + f.source_ids)
            print(f"  - {f.statement}" + (f"\n      evidence: {refs}" if refs else ""))
        for e in extra or []:
            print(f"  - {e}")
        if not items and not extra:
            print("  (none)")

    section("Decisions needed", {"decision"})
    section("Unknown, to verify", {"unknown"}, r.missing_information)
    section("Inconsistencies between books", {"inconsistency"})
    section("Facts and calculations", {"fact", "calculation"})
    print("\nCost entries")
    for ln in r.cost_lines:
        print(
            f"  {ln.ref:<9}{ln.date or '':<11}{ln.category:<10}{inr(ln.charged_paise):>11}  "
            f"-> {ln.charged_to_ref:<7} {(ln.description or '')[:42]:<42} "
            f"{(ln.counterparty or '')[:24]:<24} {' '.join(map(_short_id, ln.source_ids))}"
        )
    print("\nHow the records connect (graph paths)")
    for path in r.evidence_paths:
        hops = "; ".join(
            f"{_short_id(e.from_id)} -{e.type}-> {_short_id(e.to_id)}"
            + (f" [{_short_id(e.source_id)}]" if e.source_id else "")
            for e in path.edges
        )
        print(f"  {path.id}: {hops}")
    print("\nSource records")
    for src in r.sources:
        print(
            f"  {src.relation:<11} {_short_id(src.about):<14} {src.id} '{src.name}' [{src.status}]"
        )
    print("\nWarnings")
    for warning in r.warnings:
        print(f"  ! {warning}")
    print("\nCalculation rules")
    for rule in r.calculation_rules:
        print(f"  * {rule}")
    print(f"\nGraph queries run: {', '.join(r.queries_executed)}")


def print_overview(o: OverviewResponse) -> None:
    print(f"\nContracts of {o.company_id} (provisional, recorded direct costs only)")
    print(
        f"  {'contract':<9}{'customer':<22}{'status':<15}{'value':>13}{'cost':>13}"
        f"{'profit':>13}{'margin':>8}{'to receive':>13}  issues"
    )
    for c in o.contracts:
        print(
            f"  {c.ref:<9}{(c.customer or '')[:21]:<22}{c.status or '':<15}{inr(c.value_paise):>13}"
            f"{inr(c.recorded_cost_paise):>13}{inr(c.provisional_profit_paise):>13}"
            f"{(c.provisional_margin_percent or '-') + '%':>8}"
            f"{inr(c.outstanding_receivable_paise):>13}"
            f"  {'; '.join(c.issues) or '-'}"
        )
    print(f"\n  Workshop costs not charged to any contract: {inr(o.unallocated_overhead_paise)}")


def print_unallocated(u: UnallocatedResponse) -> None:
    print(f"\nCosts not (fully) charged to any contract: {inr(u.total_unallocated_paise)}")
    for i in u.items:
        noted = f" noted for {', '.join(i.noted_for)}" if i.noted_for else ""
        print(
            f"  {i.ref:<9}{i.classification:<12}{inr(i.unallocated_paise):>11}  "
            f"{i.description} ({i.counterparty or 'no counterparty'}){noted}"
        )
    for pmt in u.unapplied_payments:
        print(
            f"  {pmt['ref']:<9}{'payment':<12}{inr(pmt['unapplied_paise']):>11}  paid to "
            f"{pmt['supplier']} on {pmt['paid_on']}, not applied to any bill"
        )


def _ct_company(args: argparse.Namespace) -> str:
    return str(args.company or CONTRACTS_COMPANY)


def cmd_ct_seed(args: argparse.Namespace) -> None:
    store, company = _store(), _ct_company(args)
    if args.reset:
        store.drop_company(company)
    summary = seed_contracts(store, company)
    print(
        f"seeded {summary.company_id}: {summary.entities} records, {summary.sources} source "
        f"records, {summary.relationships} links (idempotent; safe to re-run)"
    )


def cmd_ct_investigate(args: argparse.Namespace) -> None:
    response = ContractCostInvestigator(_store(), _ct_company(args)).investigate(
        ContractCostRequest(contract=args.contract, as_of=date.fromisoformat(args.as_of))
    )
    if args.json:
        print(response.model_dump_json(indent=2, by_alias=True))
    else:
        print_contract(response)


def cmd_ct_overview(args: argparse.Namespace) -> None:
    result = ContractCostInvestigator(_store(), _ct_company(args)).overview()
    print(result.model_dump_json(indent=2) if args.json else "", end="")
    if not args.json:
        print_overview(result)


def cmd_ct_unallocated(args: argparse.Namespace) -> None:
    result = ContractCostInvestigator(_store(), _ct_company(args)).unallocated()
    print(result.model_dump_json(indent=2) if args.json else "", end="")
    if not args.json:
        print_unallocated(result)


def _ct_summary(r: ContractCostResponse) -> dict[str, Any]:
    p = r.profitability
    return {
        "figures": [
            f"recorded direct cost {inr(p.recorded_direct_cost_paise)}",
            f"provisional profit {inr(p.provisional_profit_paise)} "
            f"({p.provisional_margin_percent}%)",
            f"unsplit costs noted for it {inr(p.pending_allocation_paise)}",
        ],
        "decisions": r.decisions_required,
        "unknown": [f.statement for f in r.findings if f.kind == "unknown"],
        "inconsistencies": [f.statement for f in r.findings if f.kind == "inconsistency"],
    }


def cmd_ct_demo(args: argparse.Namespace) -> None:
    """Seed, investigate CT-104, re-seed (no change), apply new records and compare."""
    store, company = _store(), _ct_company(args)
    store.drop_company(company)
    print("=== 1. Load Kaveri Body Works' books (synthetic) into the graph")
    cmd_ct_seed(argparse.Namespace(company=company, reset=False))
    investigator = ContractCostInvestigator(store, company)
    request = ContractCostRequest(contract="CT-104", as_of=CASHFLOW_AS_OF)
    first = investigator.investigate(request)
    print_contract(first)

    print("\n=== 2. Load the same books again: nothing changes")
    seed_contracts(store, company)
    same = _ct_summary(investigator.investigate(request)) == _ct_summary(first)
    print(f"  investigation unchanged: {same}")

    print(
        "\n=== 3. 16 Oct: cheque clears, owner splits the paint and assigns Murugan, new purchase"
    )
    seed_contracts(store, company, dataset="kaveri-updates")
    for change in investigator.changed_since("2026-10-15T18:00:00Z"):
        target = f" -> {_short_id(change['target_id'])}" if change["target_id"] else ""
        print(
            f"  {change['change']:<11}{change['ref']:<10}{change['description']} "
            f"{inr(change['amount_paise'])}{target} [{change['status']}]"
        )
    seed_contracts(store, company, dataset="kaveri-updates")  # replaying changes nothing
    after = investigator.investigate(request)
    print("  changes in the CT-104 investigation:")
    _print_diff(_ct_summary(first), _ct_summary(after))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="company-brain")
    parser.add_argument(
        "--company",
        default=None,
        help="tenant id (default: northstar; varsha-electricals for cashflow commands)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="load the synthetic Northstar Systems graph")
    p_seed.add_argument("--reset", action="store_true", help="drop the company graph first")
    p_seed.set_defaults(func=lambda a: _seed(_store(), a.company, a.reset))

    p_inv = sub.add_parser("investigate", help="run an evidence investigation")
    p_inv.add_argument("question", nargs="?", default=DEMO_QUESTION)
    p_inv.add_argument("--focus", action="append", help="focus entity id (repeatable)")
    p_inv.add_argument("--as-of", default=DEMO_AS_OF.isoformat(), help="YYYY-MM-DD")
    p_inv.add_argument("--json", action="store_true")
    p_inv.set_defaults(func=cmd_investigate)

    p_eval = sub.add_parser(
        "eval", help="compare graph retrieval with an isolated-records baseline"
    )
    p_eval.add_argument("--json", action="store_true")
    p_eval.set_defaults(func=cmd_eval)

    p_demo = sub.add_parser(
        "demo-update", help="apply change events and show how the answer shifts"
    )
    p_demo.set_defaults(func=cmd_demo_update)

    p_cf = sub.add_parser("cashflow", help="Cashflow Radar: receivables and commitments")
    cf = p_cf.add_subparsers(dest="cashflow_command", required=True)
    c_seed = cf.add_parser("seed", help="load the synthetic Varsha Electricals records")
    c_seed.add_argument("--reset", action="store_true", help="drop the company graph first")
    c_seed.set_defaults(func=cmd_cf_seed)
    c_imp = cf.add_parser("import", help="validate and import a folder of CSV files")
    c_imp.add_argument("directory")
    c_imp.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    c_imp.set_defaults(func=cmd_cf_import)
    c_inv = cf.add_parser("investigate", help="who owes money, and what depends on it")
    c_inv.add_argument("question", nargs="?", default=CashflowRequest().question)
    c_inv.add_argument("--as-of", default=CASHFLOW_AS_OF.isoformat(), help="YYYY-MM-DD")
    c_inv.add_argument("--days", type=int, default=7, help="commitment window in days (1-31)")
    c_inv.add_argument("--json", action="store_true")
    c_inv.set_defaults(func=cmd_cf_investigate)
    c_demo = cf.add_parser("demo", help="import, investigate, update and compare")
    c_demo.set_defaults(func=cmd_cf_demo)

    p_ct = sub.add_parser("contracts", help="contract cost investigation for a manufacturer")
    ct = p_ct.add_subparsers(dest="contracts_command", required=True)
    t_seed = ct.add_parser("seed", help="load the synthetic Kaveri Body Works books")
    t_seed.add_argument("--reset", action="store_true", help="drop the company graph first")
    t_seed.set_defaults(func=cmd_ct_seed)
    t_inv = ct.add_parser("investigate", help="cost, provisional profit and gaps of a contract")
    t_inv.add_argument("contract", nargs="?", default="CT-104")
    t_inv.add_argument("--as-of", default=CASHFLOW_AS_OF.isoformat(), help="YYYY-MM-DD")
    t_inv.add_argument("--json", action="store_true")
    t_inv.set_defaults(func=cmd_ct_investigate)
    t_over = ct.add_parser("overview", help="all contracts and which have incomplete costs")
    t_over.add_argument("--json", action="store_true")
    t_over.set_defaults(func=cmd_ct_overview)
    t_un = ct.add_parser("unallocated", help="costs not charged to any contract")
    t_un.add_argument("--json", action="store_true")
    t_un.set_defaults(func=cmd_ct_unallocated)
    t_demo = ct.add_parser("demo", help="seed, investigate CT-104, record changes, compare")
    t_demo.set_defaults(func=cmd_ct_demo)

    args = parser.parse_args(argv)
    if args.command not in {"cashflow", "contracts"}:
        args.company = args.company or "northstar"
    try:
        code = args.func(args)
    except GraphError as exc:
        print(f"error: {exc}. Is FalkorDB running? Try: docker compose up -d", file=sys.stderr)
        return 1
    return code if isinstance(code, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
