"""Plain-language answers over a workspace graph: tools, citations, fallback, graph labels."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from company_brain.api import create_app
from company_brain.graph import FalkorDBStore
from company_brain.tables import import_task_table
from company_brain.workspace_agent import WORKSPACE_PROMPT, ask_workspace
from tests.test_agent_and_bot import FakeModel, _text, _tool_call
from tests.test_tables import _request

KEY = "ask-key-0123456789abc"


def test_model_answers_from_tool_results_with_citations(
    real_store: FalkorDBStore, company: str
) -> None:
    import_task_table(real_store, company, _request())
    model = FakeModel(
        [_tool_call("investigate", {"question": "Why is Launch late?"}), _text("T-2 waits on T-3.")]
    )
    wa = ask_workspace(
        real_store, company, "Why is Launch late?", api_key="k", model="m", transport=model
    )
    assert wa.answer.text == "T-2 waits on T-3."
    sent = model.requests[0][2]
    assert sent["messages"][0]["content"] == WORKSPACE_PROMPT
    assert {t["function"]["name"] for t in sent["tools"]} == {"investigate", "what_needs_attention"}
    result = json.loads(model.requests[1][2]["messages"][-1]["content"])
    blocked = next(f for f in result["findings"] if f["category"] == "blocker_dependency")
    assert blocked["statement"].startswith("T-2 is blocked by T-3") and blocked["cite"] == [
        "Tasks!3"
    ]
    assert "No current owner is recorded for T-3." in result["missing_information"]
    # graph data for the page: every entity on an evidence path has a label
    edges = {
        (e.from_id, e.to_id) for r in wa.investigations for p in r.evidence_paths for e in p.edges
    }
    assert ("ticket:t-2", "ticket:t-3") in edges
    assert (
        wa.labels["ticket:t-2"]["ref"] == "T-2" and wa.labels["project:launch"]["name"] == "Launch"
    )


def test_ask_endpoint_falls_back_to_the_deterministic_answer(
    settings: Any, real_store: FalkorDBStore, company: str
) -> None:
    import_task_table(real_store, company, _request())
    app = create_app(
        settings.model_copy(update={"company_brain_api_keys": SecretStr(f"{KEY}={company}")})
    )
    r = TestClient(app).post(
        f"/v1/companies/{company}/ask",
        json={"question": "Why is Launch late?"},
        headers={"X-API-Key": KEY},
    )
    body = r.json()
    assert r.status_code == 200 and body["answer"] is None
    assert body["fallback_reason"] == "no language model is configured"
    assert body["investigations"][0]["matched_entities"][0]["id"] == "project:launch"
    assert "ticket:t-2" in body["labels"]


def test_ask_endpoint_uses_the_model_when_configured(
    settings: Any, real_store: FalkorDBStore, company: str
) -> None:
    import_task_table(real_store, company, _request())
    model = FakeModel(
        [_tool_call("what_needs_attention", {}), _text("**Launch**\n- T-2 is blocked")]
    )
    app = create_app(
        settings.model_copy(
            update={
                "company_brain_api_keys": SecretStr(f"{KEY}={company}"),
                "open_router_api_key": SecretStr("test-model-key"),
            }
        ),
        agent_transport=model,
    )
    body = (
        TestClient(app)
        .post(
            f"/v1/companies/{company}/ask",
            json={"question": "anything"},
            headers={"X-API-Key": KEY},
        )
        .json()
    )
    assert body["answer"].startswith("**Launch**") and body["fallback_reason"] is None
    assert [c["tool"] for c in body["tool_calls"]] == ["what_needs_attention"]
    assert len(body["investigations"]) == 2  # one per project
    unknown = TestClient(app).post(
        "/v1/companies/ws-nothing00000/ask", json={"question": "x"}, headers={"X-API-Key": KEY}
    )
    assert unknown.status_code == 403


def test_ask_response_edges_use_the_field_names_the_web_app_reads(
    settings: Any, real_store: FalkorDBStore, company: str
) -> None:
    import_task_table(real_store, company, _request())
    app = create_app(
        settings.model_copy(update={"company_brain_api_keys": SecretStr(f"{KEY}={company}")})
    )
    body = (
        TestClient(app)
        .post(
            f"/v1/companies/{company}/ask", json={"question": "Launch"}, headers={"X-API-Key": KEY}
        )
        .json()
    )
    edge = body["investigations"][0]["evidence_paths"][0]["edges"][0]
    assert {"type", "from_id", "to_id", "source_id"} <= set(edge)  # contract with web/ask
