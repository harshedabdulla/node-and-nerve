from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from falkordb import FalkorDB

from company_brain.config import Settings
from company_brain.graph import FalkorDBStore, GraphUnavailable, Query, Row
from company_brain.schema import graph_name
from company_brain.seed import load_dataset, seed


class FakeStore:
    """In-memory test double for unit tests. Records calls; returns canned rows."""

    def __init__(self, responses: dict[str, list[Row]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.companies: set[str] = set()
        self.available = True

    def run(self, company_id: str, query: Query, params: dict[str, Any] | None = None) -> list[Row]:
        if not isinstance(query, Query):
            raise TypeError("only named Query templates can be executed")
        self.calls.append((company_id, query.name, params or {}))
        return self.responses.get(query.name, [])

    def company_exists(self, company_id: str) -> bool:
        return company_id in self.companies

    def ensure_schema(self, company_id: str) -> None:
        self.companies.add(company_id)

    def drop_company(self, company_id: str) -> None:
        self.companies.discard(company_id)

    def ping(self) -> None:
        if not self.available:
            raise GraphUnavailable("down")


_SECRET_ENV = (
    "COMPANY_BRAIN_API_KEYS",
    "OPEN_ROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TRACE_LINK_SECRET",
)


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the developer's .env or real credentials. FALKORDB_* still apply."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _SECRET_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # session-scoped: built before the autouse fixture above applies
        falkordb_host=os.environ.get("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.environ.get("FALKORDB_PORT", "6379")),
    )


@pytest.fixture(scope="session")
def settings() -> Settings:
    return _settings()


@pytest.fixture(scope="session")
def real_store(settings: Settings) -> FalkorDBStore:
    store = FalkorDBStore(settings)
    try:
        store.ping()
    except GraphUnavailable:
        pytest.skip("FalkorDB is not running; start it with `docker compose up -d`")
    return store


@pytest.fixture
def company(real_store: FalkorDBStore) -> Iterator[str]:
    """A fresh, empty, uniquely named tenant that is dropped after the test."""
    company_id = f"test-{uuid.uuid4().hex[:10]}"
    yield company_id
    real_store.drop_company(company_id)


@pytest.fixture
def seeded(real_store: FalkorDBStore, company: str) -> str:
    seed(real_store, load_dataset(), company)
    return company


@pytest.fixture
def raw_graph(settings: Settings):  # type: ignore[no-untyped-def]
    """Direct FalkorDB access for assertions about stored data (tests only)."""
    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)

    def select(company_id: str):  # type: ignore[no-untyped-def]
        return db.select_graph(graph_name(company_id))

    return select


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "real_store" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)
