"""AI agent loop and Telegram bot, with fake HTTP transports. No real LLM or Telegram calls.

Tool results come from the real investigator against a real FalkorDB graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from company_brain.agent import (
    SYSTEM_PROMPT,
    AgentError,
    ContractAgent,
    ToolBox,
)
from company_brain.contract_cost import ContractCostInvestigator
from company_brain.graph import FalkorDBStore
from company_brain.seed import seed_contracts
from company_brain.telegram_bot import (
    Bot,
    contract_brief,
    md_to_html,
    parse_chat_ids,
    split_message,
    strip_html,
)
from company_brain.trace import refs_in, trace_link, verify
from tests.conftest import FakeStore

SECRET = "test-model-key-not-real-000"


class FakeModel:
    """Scripted chat-completions endpoint that records what it was sent."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(
        self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.requests.append((url, headers, json.loads(json.dumps(body))))
        return self.replies.pop(0)


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ]
    }


def _text(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def kaveri(real_store: FalkorDBStore, company: str) -> str:
    seed_contracts(real_store, company)
    return company


# ---------------------------------------------------------------- agent


def test_agent_runs_the_requested_tool_and_returns_the_answer(
    real_store: FalkorDBStore, kaveri: str
) -> None:
    model = FakeModel([_tool_call("investigate_contract", {"contract": "CT-104"}), _text("ok")])
    agent = ContractAgent(
        tools=ToolBox(ContractCostInvestigator(real_store, kaveri)),
        api_key=SECRET,
        model="test/model",
        transport=model,
    )
    answer = agent.ask("How much has CT-104 cost?")
    assert answer.text == "ok"
    assert answer.tool_calls == [
        {"tool": "investigate_contract", "arguments": {"contract": "CT-104"}, "error": None}
    ]
    _, headers, first = model.requests[0]
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(first)  # the key is never in the body
    assert first["messages"][0]["content"] == SYSTEM_PROMPT
    assert {t["function"]["name"] for t in first["tools"]} == {
        "investigate_contract",
        "list_contracts",
        "list_unallocated_costs",
        "list_changes_since",
    }
    tool_message = model.requests[1][2]["messages"][-1]
    result = json.loads(tool_message["content"])
    # the figures the model sees are computed by Node & Nerve, not by the model
    assert result["cost_breakdown_inr"]["total_recorded_direct_cost"] == "₹8,51,700"
    assert result["profitability_inr"]["provisional_profit"] == "₹6,78,300"
    assert result["profitability_inr"]["label"].startswith("provisional")
    assert any(f["kind"] == "decision" for f in result["findings"])
    assert "cost_lines" not in result  # compacted for the model


def test_tool_errors_are_returned_to_the_model_not_invented(
    real_store: FalkorDBStore, kaveri: str
) -> None:
    tools = ToolBox(ContractCostInvestigator(real_store, kaveri))
    assert tools.run("investigate_contract", {"contract": "CT-999"}) == {
        "error": "not found: contract:ct-999"
    }
    assert "error" in tools.run("investigate_contract", {"contract": "CT 104"})
    assert "error" in tools.run("delete_everything", {})


def test_agent_requires_a_key_and_bounds_tool_rounds() -> None:
    tools = ToolBox(ContractCostInvestigator(FakeStore(), "none"))
    with pytest.raises(AgentError, match="API key"):
        ContractAgent(tools=tools, api_key="", model="m").ask("hi")
    looping = FakeModel([_tool_call("list_contracts", {}) for _ in range(10)])
    with pytest.raises(AgentError, match="tool rounds"):
        ContractAgent(tools=tools, api_key=SECRET, model="m", transport=looping).ask("hi")
    failing = FakeModel([{"error": {"message": "rate limited"}}])
    with pytest.raises(AgentError) as exc:
        ContractAgent(tools=tools, api_key=SECRET, model="m", transport=failing).ask("hi")
    assert SECRET not in str(exc.value)


def test_example_prompt_matches_the_agent() -> None:
    text = (Path(__file__).parent.parent / "examples/agent/system_prompt.md").read_text("utf-8")
    assert SYSTEM_PROMPT in text


# ---------------------------------------------------------------- telegram


def test_unlisted_chats_get_nothing_but_their_chat_id() -> None:
    store = FakeStore()
    bot = Bot(lambda m, p: None, ContractCostInvestigator(store, "x"), None, frozenset({42}))
    for text in ["/contracts", "/contract CT-104", "What did CT-104 cost?"]:
        replies = bot.handle({"chat": {"id": 7}, "text": text})
        assert len(replies) == 1 and "chat id is <code>7</code>" in replies[0]
        assert "₹" not in replies[0]
    assert store.calls == []  # nothing was read from the graph


def test_commands_answer_from_the_graph_without_ai(real_store: FalkorDBStore, kaveri: str) -> None:
    bot = Bot(
        lambda m, p: None,
        ContractCostInvestigator(real_store, kaveri),
        None,
        frozenset({42}),
    )
    [brief] = bot.handle({"chat": {"id": 42}, "text": "/contract CT-104"})
    assert "Total ₹8,51,700" in brief and "Provisional profit ₹6,78,300" in brief
    assert "Needs your decision" in brief and "PUR-2053" in brief
    assert "<b>" in brief and "**" not in brief
    [overview] = bot.handle({"chat": {"id": 42}, "text": "/contracts@kaveri_bot"})
    assert "CT-101" in overview and "CT-105" in overview
    [unallocated] = bot.handle({"chat": {"id": 42}, "text": "/unallocated"})
    assert "SP-309" in unallocated and "WG-416" in unallocated
    assert "not configured" in bot.handle({"chat": {"id": 42}, "text": "hello"})[0]
    assert "Not found" in bot.handle({"chat": {"id": 42}, "text": "/contract CT-999"})[0]
    assert contract_brief  # imported for the CLI and tests


def test_poll_once_replies_in_the_asking_chat_and_advances_the_offset() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def call(method: str, params: dict[str, Any]) -> Any:
        sent.append((method, params))
        if method == "getUpdates":
            return [{"update_id": 10, "message": {"chat": {"id": 7}, "text": "/help"}}]
        return True

    bot = Bot(call, ContractCostInvestigator(FakeStore(), "x"), None, frozenset())
    assert bot.poll_once(timeout=0) == 1
    assert bot.offset == 11
    messages = [p for m, p in sent if m == "sendMessage"]
    assert [m["chat_id"] for m in messages] == [7]


def test_helpers() -> None:
    assert parse_chat_ids(" 42, -100123 ,") == frozenset({42, -100123})
    parts = split_message("line\n" * 2000, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("line") == 2000
    assert split_message("") == []


# ---------------------------------------------------------------- formatting and traces


def test_markdown_becomes_safe_telegram_html() -> None:
    out = md_to_html("**Unclear:**\n- PUR-2058 <script> & co [MPB p.16]\n### Note\n`SP-309`")
    assert out.splitlines() == [
        "<b>Unclear:</b>",
        "• PUR-2058 &lt;script&gt; &amp; co <i>[MPB p.16]</i>",
        "<b>Note</b>",
        "<code>SP-309</code>",
    ]
    assert strip_html(out).startswith("Unclear:")


def test_trace_links_are_signed_scoped_and_expire() -> None:
    link = trace_link("http://x", "s3cret", "kaveri", "CT-104", focus=["PUR-2058"], now=1000)
    query = dict(p.split("=", 1) for p in link.split("?", 1)[1].split("&"))
    exp, sig = int(query["exp"]), query["sig"]
    assert verify("s3cret", "kaveri", "CT-104", exp, sig, now=1000)
    assert verify("s3cret", "kaveri", "ct-104", exp, sig, now=1000)
    assert not verify("s3cret", "kaveri", "CT-105", exp, sig, now=1000)
    assert not verify("s3cret", "other", "CT-104", exp, sig, now=1000)
    assert not verify("other", "kaveri", "CT-104", exp, sig, now=1000)
    assert not verify("s3cret", "kaveri", "CT-104", exp, sig, now=exp + 1)
    assert not verify("", "kaveri", "CT-104", exp, sig, now=1000)
    assert refs_in("Pay PUR-2058 and SP-309 on CT-104; WG-416.") == [
        "CT-104",
        "PUR-2058",
        "SP-309",
        "WG-416",
    ]


def test_agent_answer_gets_trace_link_with_focus(real_store: FalkorDBStore, kaveri: str) -> None:
    model = FakeModel(
        [
            _tool_call("investigate_contract", {"contract": "CT-104"}),
            _text("**Unclear**\n- PUR-2058 [MPB p.16]"),
        ]
    )
    investigator = ContractCostInvestigator(real_store, kaveri)
    agent = ContractAgent(tools=ToolBox(investigator), api_key=SECRET, model="m", transport=model)
    bot = Bot(
        lambda m, p: None,
        investigator,
        agent,
        frozenset({42}),
        trace_url=lambda contract, focus: f"http://t/{contract}?focus={','.join(focus)}",
    )
    [reply] = bot.handle({"chat": {"id": 42}, "text": "what is unclear on CT-104?"})
    assert "<b>Unclear</b>" in reply and "• PUR-2058" in reply
    assert '<a href="http://t/CT-104?focus=PUR-2058">Evidence graph for CT-104</a>' in reply
    assert "contract investigation" in reply


def test_trace_page_requires_a_valid_signature(settings: Any, kaveri: str) -> None:
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from company_brain.api import create_app

    secret = "trace-secret-for-tests"
    client = TestClient(
        create_app(settings.model_copy(update={"trace_link_secret": SecretStr(secret)}))
    )
    link = trace_link("", secret, kaveri, "CT-104", focus=["PUR-2058"])
    page = client.get(link)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '"ref": "PUR-2058"' in page.text and "Provisional profit" in page.text
    assert "₹6,78,300" in page.text
    assert client.get(link.replace("CT-104", "CT-105")).status_code == 403
    forged = link.split("sig=")[0] + "sig=" + "0" * 32
    assert client.get(forged).status_code == 403
