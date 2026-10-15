"""HTTP end-to-end against a real FalkorDB."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from company_brain.api import create_app
from company_brain.config import Settings
from company_brain.graph import FalkorDBStore

KEY = "test-key-0123456789abcdef"


def _client(settings: Settings, company: str) -> TestClient:
    s = settings.model_copy(update={"company_brain_api_keys": SecretStr(f"{KEY}={company}")})
    client = TestClient(create_app(s))
    client.headers["X-API-Key"] = KEY
    return client


def test_http_flow_from_empty_tenant_to_investigation(
    settings: Settings, real_store: FalkorDBStore, company: str
) -> None:
    client = _client(settings, company)
    base = f"/v1/companies/{company}"
    entities: list[dict[str, Any]] = [
        {
            "id": "project:apollo",
            "name": "Project Apollo",
            "properties": {"due_date": "2026-12-01"},
        },
        {
            "id": "ticket:ap-1",
            "name": "Payments hookup",
            "properties": {"status": "blocked", "due_date": "2026-10-01"},
        },
        {"id": "ticket:ap-2", "name": "Vendor contract", "properties": {"status": "open"}},
        {"id": "incident:i-1", "name": "Vendor API outage", "properties": {"status": "open"}},
        {"id": "person:kai", "name": "Kai"},
        {"id": "decision:d-1", "name": "Single vendor only", "properties": {"status": "accepted"}},
    ]
    for body in entities:
        assert client.post(f"{base}/entities", json=body).status_code == 200
    for rel in [
        {"type": "PART_OF", "from_id": "ticket:ap-1", "to_id": "project:apollo"},
        {"type": "BLOCKED_BY", "from_id": "ticket:ap-1", "to_id": "ticket:ap-2"},
        {"type": "AFFECTS", "from_id": "incident:i-1", "to_id": "ticket:ap-2"},
        {"type": "OWNS", "from_id": "person:kai", "to_id": "ticket:ap-1"},
        {"type": "AFFECTS", "from_id": "decision:d-1", "to_id": "project:apollo"},
        {"type": "DECIDED_BY", "from_id": "decision:d-1", "to_id": "person:kai"},
    ]:
        response = client.post(f"{base}/relationships", json=rel)
        assert response.status_code == 200, response.text
    src = {
        "id": "source:status-log",
        "name": "Vendor status log",
        "source_type": "document",
        "uri": "https://example.com/status",
        "supports": ["incident:i-1"],
        "mentions": ["decision:d-1"],
        "authored_by": ["person:kai"],
    }
    assert client.post(f"{base}/sources", json=src).status_code == 200

    r = client.post(
        f"{base}/investigations",
        json={"question": "Why is Apollo late?", "as_of": "2026-10-16"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["company_id"] == company
    assert [e["id"] for e in body["matched_entities"]] == ["project:apollo"]
    incident = next(f for f in body["findings"] if f["category"] == "incident")
    assert incident["supported"] and incident["source_ids"] == ["source:status-log"]
    edges = {(e["from"], e["type"], e["to"]) for p in body["evidence_paths"] for e in p["edges"]}
    assert ("incident:i-1", "AFFECTS", "ticket:ap-2") in edges
    assert {"source:status-log"} <= {s["id"] for s in body["sources"]}
    assert "No current owner is recorded for ticket:ap-2." in body["missing_information"]

    entity = client.get(f"{base}/entities/ticket:ap-1").json()
    assert entity["entity"]["status"] == "blocked"
    assert {(x["type"], x["direction"], x["other_id"]) for x in entity["relationships"]} >= {
        ("BLOCKED_BY", "out", "ticket:ap-2"),
        ("OWNS", "in", "person:kai"),
    }

    prov = client.get(f"{base}/decisions/decision:d-1/sources").json()
    assert prov["supporting_sources"] == []
    assert [s["id"] for s in prov["mentioning_sources"]] == ["source:status-log"]
    assert "decision:d-1 has no active supporting source" in prov["warnings"]

    paths = client.get(
        f"{base}/connections", params={"from": "ticket:ap-1", "to": "incident:i-1", "max_depth": 2}
    ).json()
    assert paths[0]["hops"] == 2

    event = {
        "type": "ticket_status_changed",
        "event_id": "ev-1",
        "ticket_id": "ticket:ap-2",
        "status": "done",
        "occurred_at": "2030-01-01T00:00:00Z",
    }
    assert client.post(f"{base}/events", json=event).json()["changes"] == [
        "ticket:ap-2 status open -> done"
    ]
    after = client.post(
        f"{base}/investigations", json={"question": "Apollo", "as_of": "2026-10-16"}
    )
    assert not [f for f in after.json()["findings"] if f["category"] == "incident"]

    assert client.get(f"{base}/entities/ticket:nope").status_code == 404
    assert client.get(f"{base}/entities/not-an-id").status_code == 422
    other = client.post("/v1/companies/someone-else/investigations", json={"question": "Apollo"})
    assert other.status_code == 403


def test_service_reports_unavailable_database_instead_of_inventing_results() -> None:
    settings = Settings(falkordb_host="127.0.0.1", falkordb_port=1, falkordb_connect_timeout_s=0.5)
    client = TestClient(create_app(settings))
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
    r = client.post(
        "/v1/companies/northstar/investigations", json={"question": "Why is Atlas late?"}
    )
    assert r.status_code == 503
    assert r.json() == {"detail": "graph database unavailable"}
