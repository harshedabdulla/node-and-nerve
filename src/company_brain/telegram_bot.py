"""Telegram front end for the contract investigation (long polling, standard library).

Only chats listed in ``TELEGRAM_ALLOWED_CHAT_IDS`` get answers; anyone else is told
their chat id and nothing more, because the bot exposes company financial records.
The bot only replies in the chat that asked. It never messages customers or suppliers
and never records or changes anything.

Commands:
  /start, /help        what the bot can do (and your chat id)
  /contracts           all contracts, provisional figures (no AI)
  /contract CT-104     one contract's cost, provisional profit and gaps (no AI)
  /unallocated         costs not charged to any contract (no AI)
  any other text       answered by the AI agent using Node & Nerve tools
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from company_brain.agent import AgentError, ContractAgent
from company_brain.cashflow import inr
from company_brain.contract_cost import (
    ContractCostInvestigator,
    ContractCostRequest,
    ContractCostResponse,
)
from company_brain.graph import CompanyNotFound, GraphError
from company_brain.ingest import EntityNotFound
from company_brain.trace import refs_in

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4000  # Telegram's limit is 4096 characters

Call = Callable[[str, dict[str, Any]], Any]


def telegram_call(token: str, timeout: float = 40.0) -> Call:
    def call(method: str, params: dict[str, Any]) -> Any:
        request = urllib.request.Request(  # noqa: S310 - fixed https URL
            API.format(token=token, method=method),
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Never include the URL: it contains the bot token.
            raise RuntimeError(f"Telegram {method} failed with HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Telegram {method} unreachable: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} error: {body.get('description')}")
        return body["result"]

    return call


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text] if text else parts


_TOOL_NAMES = {
    "investigate_contract": "contract investigation",
    "list_contracts": "all contracts",
    "list_unallocated_costs": "unallocated costs",
    "list_changes_since": "recent changes",
}
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_CITE = re.compile(r"(\[[A-Z][A-Za-z]* ?[A-Za-z0-9./ -]*\])")
_TAG = re.compile(r"<[^>]+>")


def md_to_html(text: str) -> str:
    """Model Markdown (bold, bullets, code, [cites]) to Telegram HTML, escaped per line so
    that a message split on line boundaries never breaks a tag."""
    out = []
    for raw in text.splitlines():
        line = html.escape(raw.rstrip(), quote=False)
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            line = f"<b>{heading[1]}</b>"
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        line = _BOLD.sub(r"<b>\1</b>", line)
        line = _CODE.sub(r"<code>\1</code>", line)
        line = _CITE.sub(r"<i>\1</i>", line)
        out.append(line)
    return "\n".join(out).strip()


def strip_html(text: str) -> str:
    return html.unescape(_TAG.sub("", text))


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


def contract_brief(r: ContractCostResponse, trace_url: str | None = None) -> str:
    """Deterministic phone-sized card (Telegram HTML). Every figure comes from the
    investigation."""
    b, p, c = r.cost_breakdown, r.profitability, r.contract
    lines = [
        f"<b>{_e(c.ref)} · {_e(c.customer_name)}</b>",
        f"{c.body_count} {_e(c.body_type)} bodies · {_e((c.status or '').replace('_', ' '))} · "
        f"as of {r.as_of.strftime('%d %b %Y')}",
        "",
        "<b>Recorded direct cost</b> <i>(provisional)</i>",
        f"Materials {inr(b.materials_paise)}",
        f"Labour {inr(b.labour_paise)}",
        f"Machinery {inr(b.machinery_paise)}",
        f"Other {inr(b.other_paise)}",
        f"<b>Total {inr(b.total_paise)}</b> · {b.entries} entries",
        "",
        f"<b>Provisional profit {inr(p.provisional_profit_paise)} "
        f"({p.provisional_margin_percent}%)</b>",
        f"on a contract value of {inr(p.contract_value_paise)}",
    ]
    if p.pending_allocation_paise:
        lines.append(
            f"If the {inr(p.pending_allocation_paise)} not yet split all lands here: "
            f"{inr(p.profit_if_all_pending_charged_here_paise)}"
        )
    owed = [x for x in r.supplier_bills if x.outstanding_paise]
    unclear = [x for x in owed if x.reconciliation != "consistent"]
    lines += [
        "",
        "<b>Money</b>",
        f"• Customer paid {inr(r.customer_account.received_paise)} · to receive "
        f"{inr(r.customer_account.outstanding_paise)}",
    ]
    if owed:
        lines.append(
            f"• Owed to suppliers {inr(sum(x.outstanding_paise or 0 for x in owed))} on "
            f"{len(owed)} bills"
            + (
                f" ({len(unclear)} unclear: {', '.join(_e(x.ref) for x in unclear)})"
                if unclear
                else ""
            )
        )
    sections = [
        ("Needs your decision", [f.statement for f in r.findings if f.kind == "decision"]),
        (
            "To verify",
            [f.statement for f in r.findings if f.kind in {"unknown", "inconsistency"}]
            + r.missing_information,
        ),
    ]
    for title, items in sections:
        if items:
            lines += ["", f"<b>{title} ({len(items)})</b>"]
            lines += [f"• {_e(item)}" for item in items]
    if trace_url:
        lines += ["", f'🔎 <a href="{html.escape(trace_url)}">Open the evidence graph</a>']
    status_note = next((w for w in r.warnings if "still" in w), None)
    lines += [
        "",
        "<i>"
        + (_e(status_note) + " " if status_note else "")
        + "Provisional, from recorded books only; not statutory accounts.</i>",
    ]
    return "\n".join(lines)


TraceUrl = Callable[[str, list[str]], str]


@dataclass
class Bot:
    call: Call
    investigator: ContractCostInvestigator
    agent: ContractAgent | None
    allowed_chat_ids: frozenset[int]
    trace_url: TraceUrl | None = None
    offset: int = 0
    replies: list[tuple[int, str]] = field(default_factory=list)

    def _trace(self, contract: str, focus: list[str] | None = None) -> str | None:
        return self.trace_url(contract, focus or []) if self.trace_url else None

    def handle(self, message: dict[str, Any]) -> list[str]:
        """Return the replies (Telegram HTML) for one incoming message."""
        chat_id = int(message["chat"]["id"])
        text = (message.get("text") or "").strip()
        if chat_id not in self.allowed_chat_ids:
            return [
                f"This bot is private. Your chat id is <code>{chat_id}</code>. Ask the owner "
                "to add it to TELEGRAM_ALLOWED_CHAT_IDS."
            ]
        if not text:
            return ["Send a question, or /help."]
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help"}:
                return [
                    "<b>Node &amp; Nerve</b> · contract costs from your recorded books\n\n"
                    "/contracts · every contract at a glance\n"
                    "/contract CT-104 · cost, provisional profit and what is missing\n"
                    "/unallocated · costs not charged to any contract\n\n"
                    "Or just ask, for example:\n"
                    "<i>Which bills on CT-104 are still unpaid?</i>\n"
                    "<i>What should I check before billing Pon Logistics?</i>"
                ]
            if command == "/contracts":
                o = self.investigator.overview()
                blocks = ["<b>Contracts</b> <i>(provisional, recorded direct costs)</i>"]
                for c in o.contracts:
                    link = self._trace(c.ref)
                    block = [
                        f"<b>{_e(c.ref)}</b> · {_e(c.customer)} · "
                        f"{_e((c.status or '').replace('_', ' '))}",
                        f"Value {inr(c.value_paise)} · cost {inr(c.recorded_cost_paise)}",
                        f"Profit {inr(c.provisional_profit_paise)} "
                        f"({c.provisional_margin_percent}%) · to receive "
                        f"{inr(c.outstanding_receivable_paise)}",
                    ]
                    if c.issues:
                        block.append(f"<i>{_e('; '.join(c.issues))}</i>")
                    if link:
                        block.append(f'🔎 <a href="{html.escape(link)}">graph</a>')
                    blocks.append("\n".join(block))
                blocks.append(
                    f"Workshop costs not charged to any contract: "
                    f"<b>{inr(o.unallocated_overhead_paise)}</b> · /unallocated"
                )
                return ["\n\n".join(blocks)]
            if command == "/contract":
                if not arg.strip():
                    return ["Usage: <code>/contract CT-104</code>"]
                response = self.investigator.investigate(
                    ContractCostRequest(
                        contract=arg.strip(), as_of=self.agent.tools.as_of if self.agent else None
                    )
                )
                return [contract_brief(response, self._trace(response.contract.ref))]
            if command == "/unallocated":
                u = self.investigator.unallocated()
                groups = {
                    "needs_split": "Waiting for a split",
                    "unassigned": "No job recorded",
                    "overhead": "Workshop overheads",
                }
                lines = [f"<b>Not charged to any contract: {inr(u.total_unallocated_paise)}</b>"]
                for key, title in groups.items():
                    items = [i for i in u.items if i.classification == key]
                    if items:
                        lines += ["", f"<b>{title}</b>"]
                        lines += [
                            f"• {_e(i.ref)} {inr(i.unallocated_paise)} · {_e(i.description)}"
                            + (f" · for {_e(', '.join(i.noted_for))}" if i.noted_for else "")
                            for i in items
                        ]
                if u.unapplied_payments:
                    lines += ["", "<b>Payments not applied to any bill</b>"]
                    lines += [
                        f"• {_e(p['ref'])} {inr(p['unapplied_paise'])} to {_e(p['supplier'])} "
                        f"on {_e(p['paid_on'])}"
                        for p in u.unapplied_payments
                    ]
                return ["\n".join(lines)]
            if command.startswith("/"):
                return ["Unknown command. /help"]
            if self.agent is None:
                return ["The AI agent is not configured. Use /contracts or /contract CT-104."]
            answer = self.agent.ask(text)
            mentioned = refs_in(answer.text)
            contracts = list(
                dict.fromkeys(answer.contracts + [r for r in mentioned if r.startswith("CT-")])
            )[:3]
            footer = []
            for ref in contracts:
                link = self._trace(ref, [r for r in mentioned if not r.startswith("CT-")])
                if link:
                    footer.append(
                        f'🔎 <a href="{html.escape(link)}">Evidence graph for {_e(ref)}</a>'
                    )
            checked = ", ".join(
                dict.fromkeys(_TOOL_NAMES.get(c["tool"], c["tool"]) for c in answer.tool_calls)
            )
            footer.append(
                "<i>Answered from your recorded books"
                + (f" ({_e(checked)})" if checked else "")
                + ". Figures are provisional.</i>"
            )
            return [md_to_html(answer.text) + "\n\n" + "\n".join(footer)]
        except EntityNotFound as exc:
            return [f"Not found: {_e(', '.join(exc.missing))}"]
        except CompanyNotFound:
            return ["No records loaded for this company yet."]
        except GraphError:
            return ["The records database is unavailable. Try again shortly."]
        except AgentError as exc:
            log.warning("agent failed: %s", exc)
            return ["The AI agent failed. /contract CT-104 still works without it."]
        except ValueError as exc:
            return [f"Invalid request: {_e(exc)}"]

    def _send(self, chat_id: int, text: str) -> None:
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        try:
            self.call("sendMessage", params)
        except RuntimeError as exc:
            if "parse" not in str(exc).lower():
                raise
            log.warning("HTML rejected (%s); sending plain text", exc)
            self.call("sendMessage", {"chat_id": chat_id, "text": strip_html(text)})
        self.replies.append((chat_id, text))

    def poll_once(self, timeout: int = 30) -> int:
        updates = self.call(
            "getUpdates",
            {"offset": self.offset, "timeout": timeout, "allowed_updates": ["message"]},
        )
        for update in updates:
            self.offset = max(self.offset, int(update["update_id"]) + 1)
            message = update.get("message")
            if not message or "chat" not in message:
                continue
            chat_id = int(message["chat"]["id"])
            first_word = (message.get("text") or "").split(" ", 1)[0]
            log.info(
                "message from chat %s (%s): %s",
                chat_id,
                "allowed" if chat_id in self.allowed_chat_ids else "refused",
                first_word if first_word.startswith("/") else "<question>",
            )
            with contextlib.suppress(RuntimeError):  # the typing hint is optional
                self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            started = time.monotonic()
            for reply in self.handle(message):
                log.info("replying to %s after %.1fs", chat_id, time.monotonic() - started)
                for part in split_message(reply):
                    self._send(chat_id, part)
        return len(updates)

    def run_forever(self) -> None:
        log.info("Telegram bot polling; allowed chats: %s", sorted(self.allowed_chat_ids) or "none")
        while True:
            try:
                self.poll_once()
            except RuntimeError as exc:
                log.warning("%s; retrying in 5s", exc)
                time.sleep(5)


def parse_chat_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in filter(None, (p.strip() for p in raw.split(","))):
        ids.add(int(part))
    return frozenset(ids)
