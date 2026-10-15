"""Reproducible evaluation: graph investigation vs. an isolated-records baseline.

The baseline gets exactly the same synthetic records (entities and sources, with
their text) but none of the relationships. It ranks records by keyword overlap with
the question, which is roughly what retrieval over disconnected documents can do.
The point is not that keyword search is weak; it is to measure which parts of the
expected answer can only be reached by traversing relationships.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from company_brain.graph import GraphStore
from company_brain.investigate import Investigator, question_terms
from company_brain.models import InvestigationRequest, InvestigationResponse


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str
    as_of: date
    required_entities: frozenset[str]
    # (from, TYPE, to) edges that must appear together in one evidence path
    required_path: tuple[tuple[str, str, str], ...]
    required_sources: frozenset[str]
    superseded: frozenset[str]
    expected_missing: frozenset[str]
    excluded: frozenset[str]
    focus_ids: tuple[str, ...] = ()


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="atlas_delay",
        question=(
            "Why is Project Atlas behind schedule, who owns the blockers, which previous "
            "decisions are relevant, and what evidence supports the findings?"
        ),
        as_of=date(2026, 10, 16),
        required_entities=frozenset(
            {
                "project:atlas",
                "ticket:atlas-142",
                "ticket:int-88",
                "incident:inc-009",
                "decision:dec-004",
                "person:dev-patel",
                "person:sam-okafor",
                "person:priya-nair",
                "person:lena-ortiz",
            }
        ),
        required_path=(
            ("ticket:atlas-142", "PART_OF", "project:atlas"),
            ("ticket:atlas-142", "BLOCKED_BY", "ticket:int-88"),
            ("incident:inc-009", "AFFECTS", "ticket:int-88"),
            ("incident:inc-009", "SUPPORTED_BY", "source:postmortem-009"),
            ("source:postmortem-009", "MENTIONS", "decision:dec-004"),
            ("decision:dec-004", "DECIDED_BY", "person:priya-nair"),
        ),
        required_sources=frozenset(
            {"source:postmortem-009", "source:jira-atlas-142", "source:arch-review-2026-07"}
        ),
        superseded=frozenset({"decision:dec-002"}),
        expected_missing=frozenset({"ticket:atlas-147"}),
        excluded=frozenset({"ticket:beacon-21", "incident:inc-011", "decision:dec-001"}),
    ),
    Scenario(
        name="billing_gateway_expertise",
        question=(
            "Who should help with the Billing Gateway webhook failures, and what is the "
            "current guidance?"
        ),
        as_of=date(2026, 10, 16),
        required_entities=frozenset(
            {
                "component:billing-gateway",
                "ticket:int-88",
                "incident:inc-009",
                "incident:inc-007",
                "person:lena-ortiz",
                "person:sam-okafor",
                "decision:dec-004",
            }
        ),
        required_path=(
            ("ticket:int-88", "IN_COMPONENT", "component:billing-gateway"),
            ("incident:inc-009", "AFFECTS", "ticket:int-88"),
        ),
        required_sources=frozenset(
            {"source:postmortem-009", "source:postmortem-007", "source:arch-review-2026-07"}
        ),
        superseded=frozenset({"decision:dec-002"}),
        expected_missing=frozenset(),
        excluded=frozenset({"decision:dec-001", "decision:dec-005", "incident:inc-011"}),
    ),
)


@dataclass
class Score:
    system: str
    scenario: str
    entity_recall: float
    missing_entities: list[str]
    path_found: bool
    source_recall: float
    missing_sources: list[str]
    superseded_excluded: bool
    missing_evidence_flagged: bool
    leaked_entities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _ids_in(response: InvestigationResponse) -> set[str]:
    ids: set[str] = {e.id for e in response.matched_entities}
    for f in response.findings:
        ids.update(f.entity_ids)
        ids.update(f.source_ids)
    for p in response.evidence_paths:
        for e in p.edges:
            ids.update({e.from_id, e.to_id})
    ids.update(o.owner_id for o in response.current_owners)
    ids.update(o.entity_id for o in response.current_owners)
    ids.update(x.person_id for x in response.experts)
    ids.update(d.id for d in response.decisions)
    ids.update(s.id for s in response.sources)
    return ids


def score_graph(scenario: Scenario, response: InvestigationResponse) -> Score:
    ids = _ids_in(response)
    required = set(scenario.required_path)
    path_found = any(
        required <= {(e.from_id, e.type, e.to_id) for e in p.edges} for p in response.evidence_paths
    )
    supporting = {
        s.id for s in response.sources if s.relation == "supports" and s.status == "active"
    }
    current = {d.id for d in response.decisions if d.current}
    missing_text = " ".join(response.missing_information)
    return _score(
        "graph",
        scenario,
        ids,
        path_found,
        supporting,
        superseded_excluded=not (current & scenario.superseded),
        missing_flagged=all(m in missing_text for m in scenario.expected_missing),
    )


def _score(
    system: str,
    scenario: Scenario,
    ids: set[str],
    path_found: bool,
    sources: set[str],
    superseded_excluded: bool,
    missing_flagged: bool,
) -> Score:
    missing_entities = sorted(scenario.required_entities - ids)
    missing_sources = sorted(scenario.required_sources - sources)
    return Score(
        system=system,
        scenario=scenario.name,
        entity_recall=round(1 - len(missing_entities) / len(scenario.required_entities), 3),
        missing_entities=missing_entities,
        path_found=path_found,
        source_recall=round(1 - len(missing_sources) / max(1, len(scenario.required_sources)), 3),
        missing_sources=missing_sources,
        superseded_excluded=superseded_excluded,
        missing_evidence_flagged=missing_flagged,
        leaked_entities=sorted(scenario.excluded & ids),
    )


# ---------------------------------------------------------------- baseline


def _record_text(record: dict[str, Any]) -> str:
    values = [record["id"], record.get("name", ""), record.get("excerpt", "") or ""]
    values += [str(v) for v in record.get("properties", {}).values()]
    return " ".join(values).lower()


def baseline_retrieve(dataset: dict[str, Any], question: str, top_k: int = 10) -> list[str]:
    """Keyword retrieval over isolated records. No relationships are available."""
    terms = question_terms(question)
    scored = []
    for record in [*dataset["entities"], *dataset["sources"]]:
        text = _record_text(record)
        hits = sum(1 for t in terms if re.search(rf"\b{re.escape(t)}", text))
        if hits:
            scored.append((-hits, record["id"]))
    return [rid for _, rid in sorted(scored)[:top_k]]


def score_baseline(scenario: Scenario, dataset: dict[str, Any]) -> Score:
    ids = set(baseline_retrieve(dataset, scenario.question))
    decisions = {i for i in ids if i.startswith("decision:")}
    return _score(
        "baseline",
        scenario,
        ids,
        path_found=False,  # isolated records carry no relationships to form a path
        sources={i for i in ids if i.startswith("source:")},
        # without SUPERSEDES edges every retrieved decision looks equally current
        superseded_excluded=not (decisions & scenario.superseded),
        # without relationships the baseline cannot know a blocker or owner is absent
        missing_flagged=not scenario.expected_missing,
    )


def run_evaluation(
    store: GraphStore, company_id: str, dataset: dict[str, Any]
) -> list[tuple[Score, Score]]:
    results = []
    investigator = Investigator(store, company_id)
    for scenario in SCENARIOS:
        response = investigator.investigate(
            InvestigationRequest(
                question=scenario.question,
                as_of=scenario.as_of,
                focus_ids=list(scenario.focus_ids),
            )
        )
        results.append((score_graph(scenario, response), score_baseline(scenario, dataset)))
    return results
