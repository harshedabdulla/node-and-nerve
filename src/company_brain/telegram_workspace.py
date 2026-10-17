"""Telegram for workspaces: link a private chat to a workspace, then ask and get alerts.

Linking: the web app issues a one-time code and a t.me/<bot>?start=<code> link. The bot
receives "/start <code>", checks the code's hash in the control plane (unused, not
expired), and binds this chat to that workspace. Only private chats can be linked, so a
group's members cannot read a company's answers.

A linked chat gets that workspace's answers only: /attention, plain-language questions
(the deterministic investigation, no language model), /alerts on|off and /unlink. A chat
that is not linked falls through to the demo bot if it is on the demo allow-list;
anyone else is told how to link.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from company_brain.agent import AgentError
from company_brain.alerts import attention, readable
from company_brain.controlplane import ControlPlane
from company_brain.graph import CompanyNotFound, GraphError, GraphStore
from company_brain.investigate import Investigator
from company_brain.models import InvestigationRequest, InvestigationResponse
from company_brain.telegram_bot import Bot, Call, md_to_html, split_message

log = logging.getLogger(__name__)

HELP = (
    "<b>Node &amp; Nerve</b> · {name}\n\n"
    "/attention · what is blocked or overdue, by project\n"
    "Ask in plain words, for example <i>Why is the showroom late?</i>\n"
    "/alerts off · stop alerts (/alerts on to resume)\n"
    "/unlink · disconnect this chat from {name}\n\n"
    "<i>Answers come from your connected records by fixed graph queries.</i>"
)
NOT_LINKED = (
    "This chat is not linked to a Node &amp; Nerve workspace. In the app, open "
    "Integrations, then Telegram, and tap <b>Link Telegram</b>."
)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def format_findings(r: InvestigationResponse, heading: str, *, with_sources: bool) -> str:
    lines = [f"<b>{html.escape(heading)}</b>"]
    findings = [f for f in r.findings if f.category != "expertise"]
    lines += [f"• {html.escape(readable(f.statement))}" for f in findings]
    if r.missing_information:
        lines.append("<i>Not recorded:</i>")
        lines += [f"◦ {html.escape(readable(m))}" for m in r.missing_information]
    if not findings and not r.missing_information:
        lines.append("Nothing recorded needs attention here.")
    if with_sources and r.sources:
        lines.append(
            "<i>Records used:</i> " + html.escape("; ".join(s.name or s.id for s in r.sources))
        )
    return "\n".join(lines)


@dataclass
class TelegramRouter:
    call: Call
    store: GraphStore
    control: ControlPlane | None
    demo: Bot | None = None
    # (workspace id, question) -> answer text written by the language model; optional.
    answer: Callable[[str, str], str] | None = None

    # ------------------------------------------------------------ transport

    def process_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message or "chat" not in message:
            return
        chat_id = int(message["chat"]["id"])
        try:
            replies = self.handle(message)
        except Exception as exc:  # one bad message must not stop the bot
            log.exception("telegram: failed to answer chat %s: %s", chat_id, type(exc).__name__)
            replies = ["Something went wrong answering that. Try again shortly."]
        if replies is None:
            if self.demo is not None:
                self.demo.process_update(update)
            return
        for reply in replies:
            for part in split_message(reply):
                self.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": part,
                        "parse_mode": "HTML",
                        "link_preview_options": {"is_disabled": True},
                    },
                )

    # ------------------------------------------------------------ routing

    def handle(self, message: dict[str, Any]) -> list[str] | None:
        """Replies for a message, or None to hand it to the demo bot."""
        if self.control is None:
            return None
        chat = message["chat"]
        chat_id = int(chat["id"])
        text = (message.get("text") or "").strip()
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        if command == "/start" and arg.strip():
            if chat.get("type") != "private":
                return ["Link Node &amp; Nerve from a private chat with the bot, not a group."]
            return [self._link(chat_id, arg.strip(), message.get("from") or {})]

        linked = self.control.query(
            "SELECT t.workspace_id, w.name, t.alerts, t.user_id FROM telegram_chats t"
            " JOIN workspaces w ON w.id = t.workspace_id WHERE t.chat_id = $1",
            [chat_id],
        )
        if not linked:
            if self.demo is not None and chat_id in self.demo.allowed_chat_ids:
                return None
            return [NOT_LINKED]
        ws, name = str(linked[0]["workspace_id"]), str(linked[0]["name"])
        actor = f"telegram:{linked[0]['user_id']}"
        with contextlib.suppress(GraphError):
            if command in {"/start", "/help", ""}:
                return [HELP.format(name=html.escape(name))]
            if command == "/attention":
                return [self._attention(ws, name)]
            if command == "/alerts":
                on = arg.strip().lower() != "off"
                self.control.query(
                    "UPDATE telegram_chats SET alerts = $2 WHERE chat_id = $1", [chat_id, on]
                )
                return [f"Alerts for {html.escape(name)} are {'on' if on else 'off'}."]
            if command == "/unlink":
                self.control.query("DELETE FROM telegram_chats WHERE chat_id = $1", [chat_id])
                self._audit(ws, actor, "telegram.unlinked", {})
                return [f"This chat is no longer linked to {html.escape(name)}."]
            if command.startswith("/"):
                return [HELP.format(name=html.escape(name))]
            self._audit(ws, actor, "question.asked", {"question": text[:500], "via": "telegram"})
            return [self._ask(ws, text[:500])]
        return ["The records database is unavailable. Try again shortly."]

    # ------------------------------------------------------------ actions

    def _link(self, chat_id: int, code: str, sender: dict[str, Any]) -> str:
        assert self.control is not None
        claimed = self.control.query(
            "UPDATE telegram_link_codes SET used_at = now() WHERE code_hash = $1"
            " AND used_at IS NULL AND expires_at > now() RETURNING workspace_id, user_id",
            [code_hash(code)],
        )
        if not claimed:
            return (
                "This link has expired or was already used. Create a new one in Node &amp; "
                "Nerve under Integrations, then Telegram."
            )
        ws, user = str(claimed[0]["workspace_id"]), str(claimed[0]["user_id"])
        username = sender.get("username") or sender.get("first_name")
        self.control.query(
            "INSERT INTO telegram_chats (chat_id, workspace_id, user_id, username)"
            " VALUES ($1, $2, $3, $4) ON CONFLICT (chat_id) DO UPDATE SET"
            " workspace_id = EXCLUDED.workspace_id, user_id = EXCLUDED.user_id,"
            " username = EXCLUDED.username, alerts = true, linked_at = now()",
            [chat_id, ws, user, username],
        )
        self._audit(ws, user, "telegram.linked", {"username": username})
        name = self.control.query("SELECT name FROM workspaces WHERE id = $1", [ws])[0]["name"]
        return (
            f"Linked to <b>{html.escape(str(name))}</b>. You will get an alert here when work "
            "becomes blocked or overdue.\n\n" + HELP.format(name=html.escape(str(name)))
        )

    def _attention(self, ws: str, name: str) -> str:
        try:
            results = attention(self.store, ws, None)
        except CompanyNotFound:
            return f"Nothing has been synced into {html.escape(name)} yet."
        if not results:
            return "No projects are recorded yet, so there is nothing to group work by."
        return "\n\n".join(
            format_findings(
                r,
                r.matched_entities[0].name or "Project" if r.matched_entities else "Project",
                with_sources=False,
            )
            for r in results
        )

    def _ask(self, ws: str, question: str) -> str:
        if self.answer is not None:
            try:
                text = self.answer(ws, question)
                return (
                    md_to_html(text)
                    + "\n\n<i>Written by AI from your records. Ask the app for the evidence.</i>"
                )
            except AgentError as exc:
                log.warning("telegram: model failed, answering without it: %s", exc)
            except CompanyNotFound:
                return "Nothing has been synced into this workspace yet."
        try:
            r = Investigator(self.store, ws).investigate(InvestigationRequest(question=question))
        except CompanyNotFound:
            return "Nothing has been synced into this workspace yet."
        matched = ", ".join(m.name or m.id for m in r.matched_entities)
        if not matched:
            return (
                "Nothing in your records matched that question. Name a project, task or "
                "person, or send /attention."
            )
        return format_findings(r, f"About {matched}", with_sources=True)

    def _audit(self, ws: str, actor: str, action: str, detail: dict[str, Any]) -> None:
        assert self.control is not None
        self.control.query(
            "INSERT INTO audit_log (workspace_id, actor, action, detail)"
            " VALUES ($1, $2, $3, $4::jsonb)",
            [ws, actor, action, json.dumps(detail)],
        )
