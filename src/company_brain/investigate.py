"""Evidence-backed investigation built entirely from fixed graph queries.

No LLM is involved. The investigator:
  1. resolves the focus (project, component or ticket) from ids or question terms,
  2. finds blocked or overdue work and its open blockers,
  3. traverses blocker chains to incidents (bounded),
  4. collects current owners, expertise and decisions (excluding superseded ones),
  5. attaches sources, separating "supports" from "mentions"/"contradicts",
  6. reports what the graph does not contain as missing information.

Every statement in the response is rendered from a graph row and points at the
entities, paths and sources it came from.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from company_brain import queries as q
from company_brain.graph import CompanyNotFound, GraphStore, Query, Row
from company_brain.models import (
    DecisionRef,
    EntityRef,
    EvidencePath,
    Expert,
    Finding,
    InvestigationRequest,
    InvestigationResponse,
    Owner,
    PathEdge,
    SourceRef,
)
from company_brain.schema import Kind, kind_of

EXPERTISE_WEIGHTS = {
    "resolved_incident": 3,
    "owns_component": 2,
    "authored_incident_source": 1,
    "member_of_owning_team": 1,
}

_STOPWORDS = frozenset(
    (
        "the and for with why who what which how does did are was were is our their this "
        "that from into about behind schedule owns own owner owners blockers blocker "
        "blocked previous decisions decision relevant evidence support supports findings "
        "project projects late delayed delay ticket tickets component should help fix "
        "current guidance"
    ).split()
)


def question_terms(question: str) -> list[str]:
    """Lowercase content words used to match entity names. Deterministic, no NLP."""
    words = re.findall(r"[a-z0-9][a-z0-9-]{2,}", question.lower())
    seen: dict[str, None] = {}
    for word in words:
        if word not in _STOPWORDS:
            seen.setdefault(word, None)
    return list(seen)[:12]


def _edge(rel: dict[str, Any]) -> PathEdge:
    return PathEdge(
        type=rel["type"],
        from_id=rel["from"],
        to_id=rel["to"],
        source_id=rel.get("source_id"),
    )


@dataclass
class _Run:
    """Accumulates the result while the investigation executes."""

    store: GraphStore
    company_id: str
    executed: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    paths: list[EvidencePath] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def run(self, query: Query, params: dict[str, Any]) -> list[Row]:
        if query.name not in self.executed:
            self.executed.append(query.name)
        return self.store.run(self.company_id, query, params)

    def add_path(self, description: str, edges: list[PathEdge]) -> str:
        key = [(e.type, e.from_id, e.to_id) for e in edges]
        for existing in self.paths:
            if [(e.type, e.from_id, e.to_id) for e in existing.edges] == key:
                return existing.id
        path_id = f"path-{len(self.paths) + 1}"
        self.paths.append(EvidencePath(id=path_id, description=description, edges=edges))
        return path_id

    def add_finding(self, **kwargs: Any) -> None:
        self.findings.append(Finding(id=f"finding-{len(self.findings) + 1}", **kwargs))


class Investigator:
    def __init__(self, store: GraphStore, company_id: str) -> None:
        self.store = store
        self.company_id = company_id

    def investigate(self, request: InvestigationRequest) -> InvestigationResponse:
        if not self.store.company_exists(self.company_id):
            raise CompanyNotFound(self.company_id)
        as_of = request.as_of or date.today()
        run = _Run(self.store, self.company_id)

        matched, focus = self._resolve_focus(run, request)
        if not focus:
            run.missing.append(
                "No project, component or ticket in the graph matches the question. "
                "Pass focus_ids to investigate a specific entity."
            )
            return self._response(request, as_of, run, matched, {}, [], [], [])

        # -- scope and problem work ------------------------------------------------
        scope: dict[str, str] = {}  # ticket id -> how it relates to the focus
        for fid in focus:
            if kind_of(fid) is Kind.TICKET:
                scope[fid] = "focus"
            for row in run.run(q.TICKETS_IN_SCOPE, {"scope_id": fid}):
                scope.setdefault(row["id"], f"{row['via']}:{fid}")

        statuses = self._ticket_status(run, sorted(scope), as_of)
        problems = [
            s
            for s in statuses.values()
            if s["blockers"] or s["overdue"] or s["ticket"].get("status") == "blocked"
        ]
        problem_ids = [p["ticket"]["id"] for p in problems]
        blocker_ids = sorted({b["id"] for p in problems for b in p["blockers"]})
        blocker_status = self._ticket_status(
            run, [b for b in blocker_ids if kind_of(b) is Kind.TICKET], as_of
        )

        if scope and not problems:
            run.missing.append(
                f"No blocked or overdue tickets are recorded for {', '.join(focus)} as of {as_of}."
            )
        if not scope:
            run.missing.append(f"No tickets are linked to {', '.join(focus)}.")

        # -- incidents reachable from problem tickets ------------------------------
        incident_paths = (
            run.run(
                q.TICKET_INCIDENT_PATHS,
                {"ticket_ids": problem_ids, "closed": q.CLOSED_STATUSES},
            )
            if problem_ids
            else []
        )
        incident_ids = sorted({row["incident_id"] for row in incident_paths})
        incidents = (
            {row["incident"]["id"]: row for row in run.run(q.INCIDENTS, {"ids": incident_ids})}
            if incident_ids
            else {}
        )

        # -- components, decisions, experts ----------------------------------------
        components = sorted(
            {fid for fid in focus if kind_of(fid) is Kind.COMPONENT}
            | {
                c
                for s in statuses.values()
                if s["ticket"]["id"] in problem_ids
                for c in s["components"]
            }
            | {c for s in blocker_status.values() for c in s["components"]}
            | {c for i in incidents.values() for c in i["components"]}
        )
        targets = sorted(set(focus) | set(components))
        decision_rows = run.run(q.DECISIONS_AFFECTING, {"target_ids": targets})
        current_ids = {r["id"] for r in run.run(q.CURRENT_DECISIONS, {"target_ids": targets})}
        decisions = [
            DecisionRef(
                id=r["decision"]["id"],
                name=r["decision"].get("name"),
                status=r["decision"].get("status"),
                decided_at=r["decision"].get("decided_at"),
                decided_by=r["decided_by"],
                affects=sorted(r["affects"]),
                supersedes=sorted(r["supersedes"]),
                superseded_by=sorted(r["superseded_by"]),
                current=r["decision"]["id"] in current_ids,
            )
            for r in decision_rows
        ]
        experts = self._experts(run, components) if components else []

        # -- owners and sources ----------------------------------------------------
        owned_ids = sorted(set(focus) | set(problem_ids) | set(blocker_ids) | set(components))
        owners = [
            Owner(
                **{
                    k: row[k]
                    for k in (
                        "entity_id",
                        "owner_id",
                        "owner_kind",
                        "owner_name",
                        "owner_teams",
                        "source_id",
                    )
                }
            )
            for row in run.run(q.CURRENT_OWNERS, {"ids": owned_ids})
        ]
        owners_by_entity: dict[str, list[Owner]] = defaultdict(list)
        for owner in owners:
            owners_by_entity[owner.entity_id].append(owner)

        evidence_ids = sorted(
            set(problem_ids) | set(blocker_ids) | set(incident_ids) | {d.id for d in decisions}
        )
        sources = (
            [
                SourceRef(relation=row["relation"], about=row["entity_id"], **row["source"])
                for row in run.run(q.SOURCES_FOR, {"ids": evidence_ids})
            ]
            if evidence_ids
            else []
        )
        sources.sort(key=lambda s: (s.about, s.relation, s.id))
        support = self._support_index(run, sources)

        # -- findings and paths ----------------------------------------------------
        explained = {
            r["ticket_id"] for r in incident_paths if r["relation"] == "affects_blocker_chain"
        }
        for p in problems:
            self._problem_findings(run, p, scope, as_of, support, owners_by_entity, explained)
        self._incident_findings(run, incident_paths, incidents, sources, support, scope, decisions)
        for fid in focus:
            self._owner_finding(run, fid, owners_by_entity.get(fid, []))
        self._decision_findings(run, decisions, support)
        for expert in experts[:3]:
            run.add_finding(
                category="expertise",
                statement=f"{expert.person_id} ({expert.name}) has recorded experience with "
                + ", ".join(f"{b['basis']} {b['evidence_id']}" for b in expert.basis),
                entity_ids=[expert.person_id, *sorted({b["component_id"] for b in expert.basis})],
                source_ids=(cited := self._expertise_sources(expert, support)),
                supported=bool(cited),
            )
        if problems and not incident_ids:
            run.missing.append("No incident in the graph is linked to the blocked or overdue work.")
        if targets and not decisions:
            run.missing.append(f"No decisions are recorded as affecting {', '.join(targets)}.")

        return self._response(
            request, as_of, run, matched, owners_by_entity, sources, experts, decisions
        )

    # ------------------------------------------------------------ steps

    def _resolve_focus(
        self, run: _Run, request: InvestigationRequest
    ) -> tuple[list[EntityRef], list[str]]:
        if request.focus_ids:
            found = {r["id"]: r for r in run.run(q.ENTITIES_EXIST, {"ids": request.focus_ids})}
            for fid in request.focus_ids:
                if fid not in found:
                    run.warnings.append(f"focus entity {fid} does not exist in this company graph")
            refs = [
                EntityRef(
                    id=i,
                    kind=found[i]["kind"],
                    name=found[i].get("name"),
                    match_reason="requested focus",
                )
                for i in request.focus_ids
                if i in found
            ]
            return refs, [r.id for r in refs]

        terms = question_terms(request.question)
        if not terms:
            return [], []
        rows = run.run(
            q.RESOLVE_ENTITIES,
            {
                "terms": terms,
                "kinds": [Kind.PROJECT.value, Kind.COMPONENT.value, Kind.TICKET.value],
            },
        )
        refs = [
            EntityRef(
                id=r["id"],
                kind=r["kind"],
                name=r["name"],
                match_reason="name or id matches "
                + ", ".join(repr(t) for t in sorted(r["matched_terms"])),
            )
            for r in rows
        ]
        # Prefer the broadest kind that matched: project, then component, then ticket.
        for kind in (Kind.PROJECT, Kind.COMPONENT, Kind.TICKET):
            group = [r for r in rows if r["kind"] == kind.value]
            if group:
                best = max(len(r["matched_terms"]) for r in group)
                focus = sorted(r["id"] for r in group if len(r["matched_terms"]) == best)[:3]
                if len(group) > len(focus):
                    run.warnings.append(
                        f"question also matched {len(group) - len(focus)} other {kind.value}(s); "
                        "pass focus_ids to choose explicitly"
                    )
                return refs, focus
        return refs, []

    def _ticket_status(self, run: _Run, ids: list[str], as_of: date) -> dict[str, Row]:
        if not ids:
            return {}
        rows = run.run(
            q.TICKET_STATUS,
            {
                "ticket_ids": ids,
                "as_of": as_of.isoformat(),
                "closed": q.CLOSED_STATUSES,
            },
        )
        return {row["ticket"]["id"]: row for row in rows}

    def _experts(self, run: _Run, components: list[str]) -> list[Expert]:
        basis: dict[str, list[dict[str, str]]] = defaultdict(list)
        names: dict[str, str | None] = {}
        for row in run.run(q.EXPERTS_FOR_COMPONENTS, {"component_ids": components}):
            item = {k: row[k] for k in ("basis", "component_id", "evidence_id")}
            if item not in basis[row["person_id"]]:
                basis[row["person_id"]].append(item)
            names[row["person_id"]] = row["name"]
        experts = [
            Expert(
                person_id=pid,
                name=names[pid],
                score=sum(EXPERTISE_WEIGHTS[b["basis"]] for b in items),
                basis=sorted(
                    items,
                    key=lambda b: (-EXPERTISE_WEIGHTS[b["basis"]], b["evidence_id"]),
                ),
            )
            for pid, items in basis.items()
        ]
        return sorted(experts, key=lambda e: (-e.score, e.person_id))

    def _support_index(self, run: _Run, sources: list[SourceRef]) -> dict[str, list[str]]:
        """entity id -> ids of *active* sources that support it."""
        index: dict[str, list[str]] = defaultdict(list)
        for s in sources:
            if s.relation == "supports":
                if s.status == "active":
                    index[s.about].append(s.id)
                else:
                    run.warnings.append(
                        f"{s.id} was {s.status}; it is not counted as evidence for {s.about}"
                    )
            elif s.relation == "contradicts":
                run.warnings.append(f"{s.id} contradicts {s.about}; review before relying on it")
        return index

    def _problem_findings(
        self,
        run: _Run,
        p: Row,
        scope: dict[str, str],
        as_of: date,
        support: dict[str, list[str]],
        owners: dict[str, list[Owner]],
        explained: set[str],
    ) -> None:
        t = p["ticket"]
        tid = t["id"]
        base_edges = self._scope_edges(tid, scope)
        state = []
        if t.get("status") == "blocked" or p["blockers"]:
            state.append("blocked")
        if p["overdue"]:
            state.append(f"overdue (due {t.get('due_date')}, as of {as_of})")
        run.add_finding(
            category="overdue_work" if p["overdue"] else "blocked_work",
            statement=f"{tid} '{t.get('name')}' is {' and '.join(state)}; "
            f"status: {t.get('status') or 'not recorded'}",
            entity_ids=[tid],
            path_ids=[run.add_path(f"{tid} is in scope", base_edges)] if base_edges else [],
            source_ids=support.get(tid, []),
            supported=bool(support.get(tid)),
        )
        for b in sorted(p["blockers"], key=lambda b: b["id"]):
            edge = PathEdge(
                type="BLOCKED_BY",
                from_id=tid,
                to_id=b["id"],
                source_id=b.get("edge_source_id"),
            )
            path_id = run.add_path(f"{tid} is blocked by {b['id']}", [*base_edges, edge])
            run.add_finding(
                category="blocker_dependency",
                statement=f"{tid} is blocked by {b['id']} '{b.get('name')}' "
                f"(status: {b.get('status') or 'not recorded'})",
                entity_ids=[tid, b["id"]],
                path_ids=[path_id],
                source_ids=[b["edge_source_id"]] if b.get("edge_source_id") else [],
                supported=bool(b.get("edge_source_id")),
            )
        unexplained = not p["blockers"] and tid not in explained
        if p["overdue"] and unexplained and t.get("status") != "blocked":
            run.missing.append(
                f"{tid} is overdue but the graph records no open blocker, incident or status "
                "explaining the delay."
            )
        if t.get("status") == "blocked" and not p["blockers"]:
            run.missing.append(
                f"{tid} is marked blocked but no open blocker is recorded; its status may be stale."
            )
        for eid in [tid] + [b["id"] for b in p["blockers"]]:
            self._owner_finding(run, eid, owners.get(eid, []))

    def _owner_finding(self, run: _Run, eid: str, current: list[Owner]) -> None:
        if any(eid in f.entity_ids for f in run.findings if f.category == "ownership"):
            return
        if not current:
            run.missing.append(f"No current owner is recorded for {eid}.")
            return
        teams = sorted({t for o in current for t in o.owner_teams})
        run.add_finding(
            category="ownership",
            statement=f"{eid} is currently owned by "
            + ", ".join(f"{o.owner_id} ({o.owner_name})" for o in current)
            + (f"; owner team membership: {', '.join(teams)}" if teams else ""),
            entity_ids=[eid] + [o.owner_id for o in current],
            source_ids=(cited := sorted({o.source_id for o in current if o.source_id})),
            supported=bool(cited),
        )

    def _expertise_sources(self, expert: Expert, support: dict[str, list[str]]) -> list[str]:
        cited: set[str] = set()
        for b in expert.basis:
            if b["basis"] == "authored_incident_source":
                cited.add(b["evidence_id"])
            elif b["basis"] == "resolved_incident":
                cited.update(support.get(b["evidence_id"], []))
        return sorted(cited)

    def _scope_edges(self, tid: str, scope: dict[str, str]) -> list[PathEdge]:
        via = scope.get(tid, "focus")
        if ":" not in via or via == "focus":
            return []
        rel, target = via.split(":", 1)
        return [PathEdge(type=rel, from_id=tid, to_id=target)]

    def _incident_findings(
        self,
        run: _Run,
        incident_paths: list[Row],
        incidents: dict[str, Row],
        sources: list[SourceRef],
        support: dict[str, list[str]],
        scope: dict[str, str],
        decisions: list[DecisionRef],
    ) -> None:
        current = {d.id: d for d in decisions if d.current}
        mentions = defaultdict(list)  # source id -> decision ids it mentions
        for s in sources:
            if s.relation == "mentions" and s.about in current:
                mentions[s.id].append(s.about)

        direct = {
            (r["ticket_id"], r["incident_id"])
            for r in incident_paths
            if r["relation"] == "affects_blocker_chain"
        }
        ordered = sorted(
            incident_paths,
            key=lambda r: (
                r["relation"] != "affects_blocker_chain",
                r["ticket_id"],
                r["incident_id"],
            ),
        )
        for row in ordered:
            iid, tid = row["incident_id"], row["ticket_id"]
            if row["relation"] == "same_component" and (tid, iid) in direct:
                continue  # already reported through the stronger blocker-chain link
            info = incidents.get(iid, {})
            inc = info.get("incident", {"id": iid})
            edges = self._scope_edges(tid, scope) + [_edge(r) for r in row["rels"]]
            supporting = support.get(iid, [])
            path_ids = [run.add_path(f"{tid} connects to {iid} ({row['relation']})", edges)]
            # Extend the chain: incident -> supporting source -> mentioned decision -> decider.
            for sid in supporting:
                for did in mentions.get(sid, []):
                    chain = (
                        edges
                        + [
                            PathEdge(type="SUPPORTED_BY", from_id=iid, to_id=sid),
                            PathEdge(type="MENTIONS", from_id=sid, to_id=did),
                        ]
                        + [
                            PathEdge(type="DECIDED_BY", from_id=did, to_id=p["id"])
                            for p in current[did].decided_by
                        ]
                    )
                    path_ids.append(run.add_path(f"evidence chain from {tid} to {did}", chain))
            if row["relation"] == "affects_blocker_chain":
                statement = (
                    f"{iid} '{inc.get('name')}' ({inc.get('status')}, {inc.get('severity')}) "
                    f"affects the blocker chain of {tid}"
                )
            else:
                statement = (
                    f"{iid} '{inc.get('name')}' ({inc.get('status')}) occurred in the same "
                    f"component as {tid}'s blocker chain"
                )
                if info.get("resolved_by"):
                    statement += f"; resolved by {', '.join(info['resolved_by'])}"
            run.add_finding(
                category="incident",
                statement=statement,
                entity_ids=[iid, tid, *info.get("resolved_by", [])],
                path_ids=path_ids,
                source_ids=supporting,
                supported=bool(supporting),
            )
            if not supporting:
                run.missing.append(f"{iid} has no active supporting source record.")

    def _decision_findings(
        self, run: _Run, decisions: list[DecisionRef], support: dict[str, list[str]]
    ) -> None:
        for d in decisions:
            edges = [PathEdge(type="AFFECTS", from_id=d.id, to_id=t) for t in d.affects]
            edges += [
                PathEdge(type="DECIDED_BY", from_id=d.id, to_id=p["id"]) for p in d.decided_by
            ]
            path_id = run.add_path(f"{d.id} affects {', '.join(d.affects)}", edges)
            by = ", ".join(p["id"] for p in d.decided_by) or "an unrecorded decider"
            if d.current:
                run.add_finding(
                    category="current_decision",
                    statement=f"{d.id} '{d.name}' (decided {d.decided_at} by {by}) is current "
                    f"guidance for {', '.join(d.affects)}"
                    + (f"; it supersedes {', '.join(d.supersedes)}" if d.supersedes else ""),
                    entity_ids=[d.id, *d.affects],
                    path_ids=[path_id],
                    source_ids=support.get(d.id, []),
                    supported=bool(support.get(d.id)),
                )
                if not support.get(d.id):
                    run.missing.append(f"{d.id} has no active source supporting it.")
                if not d.decided_by:
                    run.missing.append(f"No decision owner is recorded for {d.id}.")
            else:
                reason = (
                    f"superseded by {', '.join(d.superseded_by)}"
                    if d.superseded_by
                    else f"status is {d.status}"
                )
                run.add_finding(
                    category="superseded_decision",
                    statement=f"{d.id} '{d.name}' is {reason}; excluded from current guidance",
                    entity_ids=[d.id, *d.superseded_by],
                    path_ids=[path_id],
                    source_ids=support.get(d.id, []),
                    supported=bool(support.get(d.id)),
                )

    def _response(
        self,
        request: InvestigationRequest,
        as_of: date,
        run: _Run,
        matched: list[EntityRef],
        owners: dict[str, list[Owner]],
        sources: list[SourceRef],
        experts: list[Expert],
        decisions: list[DecisionRef],
    ) -> InvestigationResponse:
        unsupported = [
            f.id
            for f in run.findings
            if not f.supported and f.category in {"incident", "current_decision"}
        ]
        if unsupported:
            run.warnings.append(
                f"{len(unsupported)} incident/decision finding(s) lack an active supporting "
                "source: " + ", ".join(unsupported)
            )
        return InvestigationResponse(
            investigation_id=f"inv_{uuid.uuid4().hex[:16]}",
            company_id=self.company_id,
            question=request.question,
            as_of=as_of,
            matched_entities=matched,
            findings=run.findings,
            evidence_paths=run.paths,
            sources=sources,
            current_owners=[o for eid in sorted(owners) for o in owners[eid]],
            experts=experts,
            decisions=decisions,
            missing_information=list(dict.fromkeys(run.missing)),
            warnings=list(dict.fromkeys(run.warnings)),
            queries_executed=run.executed,
        )
