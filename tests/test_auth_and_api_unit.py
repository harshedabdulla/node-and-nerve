"""API behaviour with an in-memory store: auth, validation, health and readiness."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from company_brain.api import create_app
from company_brain.auth import ApiKeys
from company_brain.config import Settings
from company_brain.investigate import question_terms
from tests.conftest import FakeStore

KEY_A = "key-northstar-0123456789"
KEY_ALL = "key-admin-0123456789abc"


def test_api_keys_parse_and_scope() -> None:
    keys = ApiKeys.parse(f"{KEY_A}=northstar|acme, {KEY_ALL}=*")
    assert keys.allows(KEY_A, "northstar")
    assert keys.allows(KEY_A, "acme")
    assert not keys.allows(KEY_A, "globex")
    assert keys.allows(KEY_ALL, "globex")
    assert not keys.allows("wrong-key-0123456789", "northstar")
    assert not keys.allows(None, "northstar")
    assert not ApiKeys.parse("").enabled


@pytest.mark.parametrize("raw", ["short=x", "no-equals-sign-here-123", "key-0123456789abcdef="])
def test_api_keys_reject_malformed_config(raw: str) -> None:
    with pytest.raises(ValueError):
        ApiKeys.parse(raw)


def _client(store: FakeStore, keys: str = "") -> TestClient:
    settings = Settings(company_brain_api_keys=SecretStr(keys))
    return TestClient(create_app(settings, store_factory=lambda _: store))


def test_health_is_independent_of_the_database() -> None:
    store = FakeStore()
    store.available = False
    client = _client(store)
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["status"] == "unavailable"


def test_ready_when_database_answers() -> None:
    assert _client(FakeStore()).get("/ready").json() == {"status": "ready", "falkordb": "ok"}


def test_api_key_is_scoped_to_companies() -> None:
    store = FakeStore()
    client = _client(store, f"{KEY_A}=northstar")
    body = {"id": "team:ops", "name": "Ops"}
    assert client.post("/v1/companies/northstar/entities", json=body).status_code == 403
    denied = client.post("/v1/companies/acme/entities", json=body, headers={"X-API-Key": KEY_A})
    assert denied.status_code == 403
    ok = client.post("/v1/companies/northstar/entities", json=body, headers={"X-API-Key": KEY_A})
    assert ok.status_code == 200
    # nothing was ever written to the other tenant
    assert {c[0] for c in store.calls} == {"northstar"}


@pytest.mark.parametrize("company", ["North", "a_b", "x%3BDROP"])
def test_invalid_company_ids_are_rejected_before_any_query(company: str) -> None:
    store = FakeStore()
    response = _client(store).post(
        f"/v1/companies/{company}/entities", json={"id": "team:a", "name": "A"}
    )
    assert response.status_code == 422
    assert store.calls == []


def test_writes_reject_unknown_properties_and_relationships() -> None:
    store = FakeStore()
    client = _client(store)
    bad_entity = client.post(
        "/v1/companies/northstar/entities",
        json={"id": "person:a", "name": "A", "properties": {"ssn": "x"}},
    )
    assert bad_entity.status_code == 422
    bad_rel = client.post(
        "/v1/companies/northstar/relationships",
        json={"type": "OWNS", "from_id": "ticket:a", "to_id": "person:b"},
    )
    assert bad_rel.status_code == 422
    assert store.calls == []


def test_reads_on_unknown_company_return_404() -> None:
    client = _client(FakeStore())
    response = client.post(
        "/v1/companies/nobody/investigations", json={"question": "why is atlas late"}
    )
    assert response.status_code == 404


def test_there_is_no_raw_cypher_endpoint() -> None:
    paths = _client(FakeStore()).get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "cypher" in p or "query" in p]


def test_question_terms_drop_stopwords_deterministically() -> None:
    terms = question_terms("Why is Project Atlas behind schedule, who owns the blockers?")
    assert terms == ["atlas"]
    assert question_terms("Billing Gateway webhook failures") == [
        "billing",
        "gateway",
        "webhook",
        "failures",
    ]


def test_prefix_scopes_cover_workspaces_only() -> None:
    keys = ApiKeys.parse("app-key-0123456789=ws-*,demo-key-012345678=northstar")
    assert keys.allows("app-key-0123456789", "ws-3k9x2m7q1p0a")
    assert not keys.allows("app-key-0123456789", "northstar")
    assert not keys.allows("app-key-0123456789", "kaveri-body-works")
    assert not keys.allows("app-key-0123456789", "w")
    assert not keys.allows("demo-key-012345678", "ws-3k9x2m7q1p0a")
    for bad in ["k-0123456789abcdef=w*s", "k-0123456789abcdef=*ws"]:
        with pytest.raises(ValueError, match="prefix"):
            ApiKeys.parse(bad)
