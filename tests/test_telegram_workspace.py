"""Telegram for workspaces: linking, answers from the workspace graph, alerts sent once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from company_brain.alerts import run_alerts
from company_brain.graph import FalkorDBStore
from company_brain.tables import import_task_table
from company_brain.telegram_workspace import NOT_LINKED, TelegramRouter, code_hash
from tests.conftest import FakeStore
from tests.test_tables import _request


class FakeControl:
    """In-memory stand-in for the control-plane tables the API touches."""

    def __init__(self) -> None:
        self.workspaces: dict[str, str] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.chats: dict[int, dict[str, Any]] = {}
        self.deliveries: set[tuple[str, int, str]] = set()
        self.audit: list[tuple[str, str]] = []

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        p = params or []
        if sql.startswith("UPDATE telegram_link_codes"):
            c = self.codes.get(p[0])
            if not c or c["used"] or c["expires"] < datetime.now(UTC):
                return []
            c["used"] = True
            return [{"workspace_id": c["ws"], "user_id": c["user"]}]
        if sql.startswith("INSERT INTO telegram_chats"):
            self.chats[p[0]] = {"ws": p[1], "user": p[2], "username": p[3], "alerts": True}
            return []
        if sql.startswith("SELECT name FROM workspaces"):
            return [{"name": self.workspaces[p[0]]}]
        if sql.startswith("SELECT t.workspace_id"):
            c = self.chats.get(p[0])
            return (
                [
                    {
                        "workspace_id": c["ws"],
                        "name": self.workspaces[c["ws"]],
                        "alerts": c["alerts"],
                        "user_id": c["user"],
                    }
                ]
                if c
                else []
            )
        if sql.startswith("UPDATE telegram_chats SET alerts"):
            self.chats[p[0]]["alerts"] = p[1]
            return []
        if sql.startswith("DELETE FROM telegram_chats"):
            self.chats.pop(p[0], None)
            return []
        if sql.startswith("INSERT INTO audit_log"):
            self.audit.append((p[0], p[2]))
            return []
        if sql.startswith("SELECT t.chat_id"):
            return [
                {"chat_id": cid, "name": self.workspaces[c["ws"]]}
                for cid, c in self.chats.items()
                if c["ws"] == p[0] and c["alerts"]
            ]
        if sql.startswith("INSERT INTO alert_deliveries"):
            key = (p[0], p[1], p[2])
            if key in self.deliveries:
                return []
            self.deliveries.add(key)
            return [{"fingerprint": p[2]}]
        if sql.startswith("DELETE FROM alert_deliveries"):
            self.deliveries.discard((p[0], p[1], p[2]))
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    def issue(self, ws: str, code: str, *, minutes: int = 15) -> None:
        self.codes[code_hash(code)] = {
            "ws": ws,
            "user": "user_1",
            "used": False,
            "expires": datetime.now(UTC) + timedelta(minutes=minutes),
        }


class Sent:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[tuple[int, str]] = []
        self.fail = fail

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        if method == "sendMessage":
            if self.fail:
                raise RuntimeError("Telegram sendMessage unreachable: TimeoutError")
            self.messages.append((params["chat_id"], params["text"]))
        return True


def _msg(chat_id: int, text: str, chat_type: str = "private") -> dict[str, Any]:
    return {"chat": {"id": chat_id, "type": chat_type}, "text": text, "from": {"username": "owner"}}


def test_linking_needs_a_fresh_code_and_a_private_chat() -> None:
    control = FakeControl()
    control.workspaces["ws-aaaaaaaaaaaa"] = "Acme Traders"
    control.issue("ws-aaaaaaaaaaaa", "good-code")
    control.issue("ws-aaaaaaaaaaaa", "old-code", minutes=-1)
    router = TelegramRouter(Sent(), FakeStore(), control)

    assert "private chat" in router.handle(_msg(-5, "/start good-code", "group"))[0]  # type: ignore[index]
    assert "expired" in router.handle(_msg(7, "/start old-code"))[0]  # type: ignore[index]
    linked = router.handle(_msg(7, "/start good-code"))
    assert linked and "Linked to <b>Acme Traders</b>" in linked[0]
    assert control.chats[7]["ws"] == "ws-aaaaaaaaaaaa"
    assert "expired or was already used" in router.handle(_msg(8, "/start good-code"))[0]  # type: ignore[index]
    assert ("ws-aaaaaaaaaaaa", "telegram.linked") in control.audit

    assert router.handle(_msg(9, "hello")) == [NOT_LINKED]
    assert "no longer linked" in router.handle(_msg(7, "/unlink"))[0]  # type: ignore[index]
    assert 7 not in control.chats


def test_unlinked_demo_chat_still_reaches_the_demo_bot() -> None:
    from company_brain.contract_cost import ContractCostInvestigator
    from company_brain.telegram_bot import Bot

    sent = Sent()
    demo = Bot(sent, ContractCostInvestigator(FakeStore(), "x"), None, frozenset({42}))
    router = TelegramRouter(sent, FakeStore(), FakeControl(), demo)
    assert router.handle(_msg(42, "/help")) is None  # handed to the demo bot
    router.process_update({"update_id": 1, "message": _msg(42, "/help")})
    assert sent.messages and sent.messages[0][0] == 42
    assert router.handle(_msg(43, "/help")) == [NOT_LINKED]


def test_linked_chat_answers_from_its_own_workspace_graph(
    real_store: FalkorDBStore, company: str
) -> None:
    import_task_table(real_store, company, _request())
    control = FakeControl()
    control.workspaces[company] = "Acme Traders"
    control.issue(company, "c1")
    router = TelegramRouter(Sent(), real_store, control)
    router.handle(_msg(7, "/start c1"))

    [attention] = router.handle(_msg(7, "/attention"))  # type: ignore[misc]
    assert "<b>Launch</b>" in attention and "T-2" in attention and "Vikram" in attention
    assert "ticket:" not in attention and "None" not in attention
    [answer] = router.handle(_msg(7, "Why is the Launch project late?"))  # type: ignore[misc]
    assert "About Launch" in answer and "Records used" in answer and "Launch tracker" in answer
    assert "Acme Traders" in router.handle(_msg(7, "/help"))[0]  # type: ignore[index]
    assert "off" in router.handle(_msg(7, "/alerts off"))[0]  # type: ignore[index]
    assert control.chats[7]["alerts"] is False


def test_alerts_are_sent_once_and_retried_after_a_failed_send(
    real_store: FalkorDBStore, company: str
) -> None:
    import_task_table(real_store, company, _request())
    control = FakeControl()
    control.workspaces[company] = "Acme Traders"
    control.chats[7] = {"ws": company, "user": "u", "username": "o", "alerts": True}
    control.chats[8] = {"ws": company, "user": "u", "username": "p", "alerts": False}

    failing = Sent(fail=True)
    first = run_alerts(real_store, control, failing, company)
    assert first.sent == 0 and first.errors and not control.deliveries  # un-claimed

    sent = Sent()
    second = run_alerts(real_store, control, sent, company)
    assert second.chats == 1 and second.sent == second.items > 0
    [(chat, text)] = sent.messages
    assert chat == 7 and "T-2" in text and "blocked" in text

    third = run_alerts(real_store, control, sent, company)
    assert third.sent == 0 and len(sent.messages) == 1  # nothing repeated


def test_alerts_with_no_linked_chat_do_nothing() -> None:
    run = run_alerts(FakeStore(), FakeControl(), Sent(), "ws-none00000000")
    assert (run.chats, run.sent) == (0, 0)


@pytest.mark.parametrize("command", ["/attention", "anything"])
def test_without_a_control_plane_everything_goes_to_the_demo_bot(command: str) -> None:
    assert TelegramRouter(Sent(), FakeStore(), None).handle(_msg(7, command)) is None
