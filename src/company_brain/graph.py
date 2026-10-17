"""FalkorDB adapter.

The rest of the service talks to the graph only through ``GraphStore``. A store
executes ``Query`` objects (named, fixed Cypher templates from ``queries.py``) and
never raw strings, so there is no code path that runs caller- or LLM-supplied Cypher.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import redis.exceptions
from falkordb import FalkorDB

from company_brain.config import Settings

log = logging.getLogger(__name__)

Row = dict[str, Any]


@dataclass(frozen=True)
class Query:
    """A named, fixed Cypher template. User values are always passed as ``$params``."""

    name: str
    cypher: str
    read_only: bool = True


class GraphError(Exception):
    """Base error for graph access."""


class GraphUnavailable(GraphError):
    """FalkorDB cannot be reached."""


class GraphTimeout(GraphError):
    """A query exceeded its time budget."""


class GraphConflict(GraphError):
    """A write violated a constraint (for example a duplicate id)."""


class CompanyNotFound(GraphError):
    """No graph exists for the requested company."""


class GraphStore(Protocol):
    def run(self, company_id: str, query: Query, params: dict[str, Any] | None = None) -> list[Row]:
        """Execute a named query in the company's graph and return rows as dicts."""
        ...

    def company_exists(self, company_id: str) -> bool: ...

    def ensure_schema(self, company_id: str) -> None: ...

    def drop_company(self, company_id: str) -> None: ...

    def ping(self) -> None: ...


def _to_python(value: Any) -> Any:
    # falkordb returns Node/Edge objects for whole entities; our queries project
    # maps and scalars, but normalise defensively so responses are plain JSON.
    if hasattr(value, "properties") and hasattr(value, "labels"):
        return dict(value.properties)
    if isinstance(value, dict):
        return {k: _to_python(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_python(v) for v in value]
    return value


class FalkorDBStore:
    def __init__(self, settings: Settings) -> None:
        from company_brain.schema import graph_name

        self._graph_name = graph_name
        self._settings = settings
        self._timeout_ms = settings.falkordb_query_timeout_ms
        self._client: FalkorDB | None = None
        self._schema_ready: set[str] = set()

    @property
    def _db(self) -> FalkorDB:
        """Connect lazily so the HTTP process starts (and /health works) while FalkorDB is down."""
        if self._client is None:
            s = self._settings
            try:
                self._client = FalkorDB(
                    host=s.falkordb_host,
                    port=s.falkordb_port,
                    username=s.falkordb_username or None,
                    password=s.falkordb_password.get_secret_value() or None,
                    ssl=s.falkordb_ssl,
                    socket_timeout=s.falkordb_socket_timeout_s,
                    socket_connect_timeout=s.falkordb_connect_timeout_s,
                )
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
                raise GraphUnavailable("FalkorDB is unreachable") from exc
        return self._client

    def run(self, company_id: str, query: Query, params: dict[str, Any] | None = None) -> list[Row]:
        if not isinstance(query, Query):
            raise TypeError("only named Query templates can be executed")
        graph = self._db.select_graph(self._graph_name(company_id))
        try:
            if query.read_only:
                result = graph.ro_query(query.cypher, params or {}, timeout=self._timeout_ms)
            else:
                result = graph.query(query.cypher, params or {}, timeout=self._timeout_ms)
        except redis.exceptions.ResponseError as exc:
            raise _translate(exc, query, company_id) from exc
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise GraphUnavailable("FalkorDB is unreachable") from exc
        columns = [col[1] for col in result.header]
        return [
            {col: _to_python(val) for col, val in zip(columns, row, strict=True)}
            for row in result.result_set
        ]

    def company_exists(self, company_id: str) -> bool:
        try:
            return bool(self._db.connection.exists(self._graph_name(company_id)))
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise GraphUnavailable("FalkorDB is unreachable") from exc

    def ensure_schema(self, company_id: str) -> None:
        """Create the id index and unique constraint once per graph (idempotent)."""
        name = self._graph_name(company_id)
        if name in self._schema_ready:
            return
        graph = self._db.select_graph(name)
        # A unique constraint requires a range index on the same property first.
        steps: list[Callable[[], object]] = [
            lambda: graph.query("CREATE INDEX FOR (n:Entity) ON (n.id)"),
            lambda: graph.query("CREATE INDEX FOR (n:Entity) ON (n.kind)"),
            lambda: graph.create_node_unique_constraint("Entity", "id"),
        ]
        try:
            for step in steps:
                try:
                    step()
                except redis.exceptions.ResponseError as exc:
                    if "already" not in str(exc).lower():
                        raise GraphError(f"schema setup failed: {exc}") from exc
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise GraphUnavailable("FalkorDB is unreachable") from exc
        self._schema_ready.add(name)

    def drop_company(self, company_id: str) -> None:
        name = self._graph_name(company_id)
        self._schema_ready.discard(name)
        if self.company_exists(company_id):
            self._db.select_graph(name).delete()

    def ping(self) -> None:
        """Readiness: the server answers and the FalkorDB graph module responds."""
        try:
            self._db.connection.ping()
            self._db.list_graphs()
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise GraphUnavailable("FalkorDB is unreachable") from exc
        except redis.exceptions.ResponseError as exc:
            raise GraphUnavailable(f"FalkorDB module not responding: {exc}") from exc


def _translate(exc: redis.exceptions.ResponseError, query: Query, company_id: str) -> GraphError:
    message = str(exc)
    lowered = message.lower()
    if "timed out" in lowered:
        return GraphTimeout(f"query {query.name!r} timed out")
    if "empty key" in lowered:
        return CompanyNotFound(company_id)
    if "constraint violation" in lowered:
        return GraphConflict(f"query {query.name!r} violated a constraint")
    log.error("query %s failed: %s", query.name, message)
    return GraphError(f"query {query.name!r} failed")
