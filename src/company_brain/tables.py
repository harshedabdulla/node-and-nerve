"""Import a task table (a spreadsheet of work items) into a company graph.

Company-agnostic: a header row, data rows and a column mapping chosen by the user become
tickets, people, projects and the links between them. Each row keeps a source record
pointing at the sheet and row it came from.

Rules:
- Nothing is guessed. An unrecognised status word, an ambiguous date or a blocker that
  names an id not in the table is reported as a row warning and left out, never filled in.
- Re-importing the same table writes the same ids (MERGE), so nothing is duplicated.
- Cell text is stored as data only; it is never treated as an instruction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from company_brain.graph import GraphStore
from company_brain.ingest import Ingestor
from company_brain.models import EntityInput, RelationshipInput, SourceInput
from company_brain.schema import Rel

MAX_ROWS = 5000
MAX_COLUMNS = 50
MAX_CELL = 500

STATUS_WORDS = {
    "open": "open",
    "to do": "open",
    "todo": "open",
    "not started": "open",
    "new": "open",
    "backlog": "open",
    "pending": "open",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "doing": "in_progress",
    "wip": "in_progress",
    "started": "in_progress",
    "ongoing": "in_progress",
    "blocked": "blocked",
    "on hold": "blocked",
    "waiting": "blocked",
    "stuck": "blocked",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "closed": "done",
    "finished": "done",
    "resolved": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "dropped": "cancelled",
}

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


class TaskMapping(BaseModel):
    """Which column holds what. Columns are given by their header text."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Column with a stable id for each row (e.g. TASK-12).")
    name: str = Field(description="Column with the task's title.")
    status: str | None = None
    owner: str | None = None
    due: str | None = None
    project: str | None = None
    blocked_by: str | None = Field(
        default=None, description="Column listing ids of tasks this one waits on, comma separated."
    )
    priority: str | None = None


class TaskTableImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(max_length=50, examples=["google_sheets"])
    document_id: str = Field(max_length=200, description="Spreadsheet or file id.")
    document_name: str = Field(max_length=300)
    table: str = Field(max_length=200, description="Sheet or tab name.")
    uri: str | None = Field(default=None, max_length=2000)
    header: list[str] = Field(min_length=1, max_length=MAX_COLUMNS)
    rows: list[list[Any]] = Field(max_length=MAX_ROWS)
    mapping: TaskMapping
    observed_at: datetime | None = None
    dry_run: bool = False


@dataclass
class TaskTableReport:
    rows_read: int = 0
    tasks: int = 0
    people: int = 0
    projects: int = 0
    links: int = 0
    sources: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    written: bool = False


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower()).strip("-.")
    if not s:
        return hashlib.sha1(text.encode()).hexdigest()[:12]  # noqa: S324 - an id, not security
    if len(s) > 80:
        s = s[:68].rstrip("-.") + "-" + hashlib.sha1(text.encode()).hexdigest()[:10]  # noqa: S324
    return s


def _cell(row: list[Any], index: int | None) -> str:
    if index is None or index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()[:MAX_CELL]


def parse_due(text: str) -> date | None:
    """ISO dates and unambiguous day-first or named-month dates. Anything else is None."""
    try:
        return _parse_due(text.strip())
    except ValueError:  # 31/02/2026 and the like
        return None


def _parse_due(t: str) -> date | None:
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", t)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 >= b:  # 25/10/2026: day first
            return date(y, b, a)
        if b > 12 >= a:  # 10/25/2026: month first
            return date(y, a, b)
        return None  # 05/06/2026 could be either
    m = re.fullmatch(r"(\d{1,2})[ -]([A-Za-z]{3})[A-Za-z]*[ -,]*(\d{4})", t)
    if m and m[2].lower() in _MONTHS:
        return date(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    m = re.fullmatch(r"([A-Za-z]{3})[A-Za-z]* (\d{1,2}),? (\d{4})", t)
    if m and m[1].lower() in _MONTHS:
        return date(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    return None


def import_task_table(store: GraphStore, company_id: str, req: TaskTableImport) -> TaskTableReport:
    report = TaskTableReport(rows_read=len(req.rows))
    header = [h.strip() for h in req.header]
    col: dict[str, int | None] = {}
    for key, column in req.mapping.model_dump().items():
        if column is None:
            col[key] = None
            continue
        if column not in header:
            raise ValueError(f"mapped column {column!r} is not in the header")
        col[key] = header.index(column)

    observed = req.observed_at or datetime.now(UTC)
    doc = _slug(f"{req.source_system}-{req.document_id}")[:40]
    entities: dict[str, EntityInput] = {}
    sources: list[SourceInput] = []
    rels: list[RelationshipInput] = []
    pending_blocks: list[tuple[str, str, str, int]] = []
    row_ids: dict[str, str] = {}

    for n, row in enumerate(req.rows, start=2):  # row 1 is the header
        ref = _cell(row, col["id"])
        title = _cell(row, col["name"])
        if not ref and not title:
            continue  # blank line
        if not ref:
            report.skipped_rows += 1
            report.warnings.append(f"row {n}: no id in '{req.mapping.id}', so it was skipped")
            continue
        tid = f"ticket:{_slug(ref)}"
        if tid in entities:
            report.skipped_rows += 1
            report.warnings.append(f"row {n}: id {ref!r} appears more than once; first row kept")
            continue
        props: dict[str, Any] = {
            "source_system": req.source_system,
            "source_record_id": ref,
        }
        if col["status"] is not None:
            raw = _cell(row, col["status"])
            if raw:
                status = STATUS_WORDS.get(raw.lower())
                if status:
                    props["status"] = status
                else:
                    report.warnings.append(
                        f"row {n} ({ref}): status {raw!r} not recognised; left unset"
                    )
        if col["due"] is not None:
            raw = _cell(row, col["due"])
            if raw:
                due = parse_due(raw)
                if due:
                    props["due_date"] = due.isoformat()
                else:
                    report.warnings.append(
                        f"row {n} ({ref}): due date {raw!r} is ambiguous or unreadable; left unset"
                    )
        if col["priority"] is not None and (p := _cell(row, col["priority"])):
            props["priority"] = p
        entities[tid] = EntityInput(
            id=tid, name=title or ref, properties=props, observed_at=observed
        )
        row_ids[ref.lower()] = tid

        sid = f"source:{doc}-{_slug(ref)}"[:107]
        cells = "; ".join(
            f"{h}: {_cell(row, i)}" for i, h in enumerate(header) if h and _cell(row, i)
        )
        sources.append(
            SourceInput(
                id=sid,
                name=f"{req.document_name} / {req.table}, row {n}",
                source_type="spreadsheet",
                uri=req.uri,
                excerpt=cells[:4000] or None,
                source_system=req.source_system,
                source_record_id=f"{req.table}!{n}",
                supports=[tid],
                observed_at=observed,
            )
        )

        if col["owner"] is not None and (owner := _cell(row, col["owner"])):
            pid = f"person:{_slug(owner)}"
            entities.setdefault(
                pid,
                EntityInput(
                    id=pid,
                    name=owner,
                    properties={"source_system": req.source_system},
                    observed_at=observed,
                ),
            )
            rels.append(
                RelationshipInput(
                    type=Rel.OWNS, from_id=pid, to_id=tid, source_id=sid, observed_at=observed
                )
            )
        if col["project"] is not None and (project := _cell(row, col["project"])):
            prj = f"project:{_slug(project)}"
            entities.setdefault(
                prj,
                EntityInput(
                    id=prj,
                    name=project,
                    properties={"source_system": req.source_system},
                    observed_at=observed,
                ),
            )
            rels.append(
                RelationshipInput(
                    type=Rel.PART_OF, from_id=tid, to_id=prj, source_id=sid, observed_at=observed
                )
            )
        if col["blocked_by"] is not None and (blockers := _cell(row, col["blocked_by"])):
            for b in filter(None, (x.strip() for x in re.split(r"[,;\n]", blockers))):
                pending_blocks.append((tid, b, sid, n))

    for tid, blocker, sid, n in pending_blocks:
        target = row_ids.get(blocker.lower())
        if target is None:
            report.warnings.append(
                f"row {n}: blocked by {blocker!r}, which is not an id in this table; link left out"
            )
        elif target == tid:
            report.warnings.append(f"row {n}: a task cannot block itself; link left out")
        else:
            rels.append(
                RelationshipInput(
                    type=Rel.BLOCKED_BY,
                    from_id=tid,
                    to_id=target,
                    source_id=sid,
                    observed_at=observed,
                )
            )

    report.tasks = sum(1 for e in entities if e.startswith("ticket:"))
    report.people = sum(1 for e in entities if e.startswith("person:"))
    report.projects = sum(1 for e in entities if e.startswith("project:"))
    report.links = len(rels)
    report.sources = len(sources)
    if req.dry_run:
        return report

    ingest = Ingestor(store, company_id)
    for entity in entities.values():
        ingest.upsert_entity(entity)
    for source in sources:
        ingest.register_source(source)
    for rel in rels:
        ingest.upsert_relationship(rel)
    report.written = True
    return report
