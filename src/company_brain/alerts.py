"""What needs attention in a workspace graph, and alerts about it that are sent once.

Alerts are about work that is blocked or overdue. Each one has a fingerprint from its
kind and the records involved (not its wording or date), and a delivery row per chat is
written before sending, so a finding is never sent to the same chat twice. If the send
fails, the rows are removed again so the next run retries.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from company_brain import queries as q
from company_brain.controlplane import ControlPlane
from company_brain.graph import CompanyNotFound, GraphStore
from company_brain.investigate import Investigator
from company_brain.models import InvestigationRequest, InvestigationResponse
from company_brain.telegram_bot import Call, split_message

log = logging.getLogger(__name__)

PROJECT_LIMIT = 10
ALERT_CATEGORIES = {"overdue_work", "blocked_work"}


def attention(
    store: GraphStore, company_id: str, as_of: date | None
) -> list[InvestigationResponse]:
    """The investigation for each project in the graph, focused by id."""
    if not store.company_exists(company_id):
        raise CompanyNotFound(company_id)
    projects = store.run(company_id, q.GRAPH_PROJECTS, {"limit": PROJECT_LIMIT})
    investigator = Investigator(store, company_id)
    return [
        investigator.investigate(
            InvestigationRequest(
                question=f"What needs attention in {p['name']}?", focus_ids=[p["id"]], as_of=as_of
            )
        )
        for p in projects
    ]


def readable(statement: str) -> str:
    """Engine statements name graph ids; show people by name and tasks by their reference."""
    s = re.sub(r"\b(?:person|team):[a-z0-9._-]+ \(([^)]+)\)", r"\1", statement)
    s = re.sub(r"\bticket:([a-z0-9._-]+)", lambda m: m[1].upper(), s)
    s = re.sub(
        r"\b(?:project|decision|incident|component|team|person):([a-z0-9._-]+)",
        lambda m: m[1].replace("-", " ").capitalize(),
        s,
    )
    return s.replace("in_progress", "in progress")


@dataclass(frozen=True)
class AlertItem:
    fingerprint: str
    project: str
    statement: str


def alert_items(workspace_id: str, results: list[InvestigationResponse]) -> list[AlertItem]:
    items: dict[str, AlertItem] = {}
    for r in results:
        project = r.matched_entities[0].name if r.matched_entities else None
        for f in r.findings:
            if f.category not in ALERT_CATEGORIES:
                continue
            key = f"{workspace_id}|{f.category}|{','.join(sorted(f.entity_ids))}"
            fp = hashlib.sha256(key.encode()).hexdigest()[:32]
            items.setdefault(fp, AlertItem(fp, project or "", readable(f.statement)))
    return list(items.values())


@dataclass
class AlertRun:
    chats: int = 0
    items: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)


def format_alert(workspace_name: str, items: list[AlertItem]) -> str:
    lines = [f"<b>{html.escape(workspace_name)}: needs attention</b>"]
    for project in sorted({i.project for i in items}):
        lines.append(f"\n<b>{html.escape(project or 'Other work')}</b>")
        lines += [f"• {html.escape(i.statement)}" for i in items if i.project == project]
    lines.append("\n<i>Each item is sent once. /attention shows the full picture.</i>")
    return "\n".join(lines)


def run_alerts(
    store: GraphStore,
    control: ControlPlane,
    call: Call,
    workspace_id: str,
    as_of: date | None = None,
) -> AlertRun:
    run = AlertRun()
    chats = control.query(
        "SELECT t.chat_id, w.name FROM telegram_chats t JOIN workspaces w ON w.id = t.workspace_id"
        " WHERE t.workspace_id = $1 AND t.alerts",
        [workspace_id],
    )
    run.chats = len(chats)
    if not chats:
        return run
    try:
        items = alert_items(workspace_id, attention(store, workspace_id, as_of))
    except CompanyNotFound:
        return run
    run.items = len(items)
    for chat in chats:
        chat_id = int(chat["chat_id"])
        new: list[AlertItem] = []
        for item in items:
            claimed = control.query(
                "INSERT INTO alert_deliveries (workspace_id, chat_id, fingerprint, statement)"
                " VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING fingerprint",
                [workspace_id, chat_id, item.fingerprint, item.statement],
            )
            if claimed:
                new.append(item)
        if not new:
            continue
        try:
            for part in split_message(format_alert(str(chat["name"]), new)):
                call("sendMessage", {"chat_id": chat_id, "text": part, "parse_mode": "HTML"})
            run.sent += len(new)
        except RuntimeError as exc:
            log.warning("alert to chat %s failed: %s", chat_id, exc)
            run.errors.append(f"chat {chat_id}: {exc}")
            for item in new:  # un-claim, so the next run retries
                control.query(
                    "DELETE FROM alert_deliveries WHERE workspace_id = $1 AND chat_id = $2"
                    " AND fingerprint = $3",
                    [workspace_id, chat_id, item.fingerprint],
                )
    return run
