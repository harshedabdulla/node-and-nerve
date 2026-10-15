"""Command-line entry point: seed, investigate, evaluate and run the update demo."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from typing import Any

from company_brain.config import get_settings
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
from company_brain.seed import load_dataset, seed

DEMO_QUESTION = (
    "Why is Project Atlas behind schedule, who owns the blockers, which previous "
    "decisions are relevant, and what evidence supports the findings?"
)
DEMO_AS_OF = date(2026, 10, 16)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="company-brain")
    parser.add_argument("--company", default="northstar", help="tenant id (default: northstar)")
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

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except GraphError as exc:
        print(f"error: {exc}. Is FalkorDB running? Try: docker compose up -d", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
