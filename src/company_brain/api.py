"""HTTP API. Thin: validation and auth here, graph work in the services."""

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from company_brain import __version__
from company_brain.auth import ApiKeys
from company_brain.config import Settings, get_settings
from company_brain.graph import (
    CompanyNotFound,
    FalkorDBStore,
    GraphConflict,
    GraphError,
    GraphStore,
    GraphTimeout,
    GraphUnavailable,
)
from company_brain.ingest import EntityNotFound, Ingestor
from company_brain.investigate import Investigator
from company_brain.lookup import Connection, DecisionProvenance, EntityView, Lookup
from company_brain.models import (
    EntityInput,
    Event,
    EventResult,
    InvestigationRequest,
    InvestigationResponse,
    RelationshipInput,
    SourceInput,
    WriteResult,
)
from company_brain.schema import COMPANY_ID_RE, KIND_PROPERTIES, RELATIONSHIP_RULES, Kind

log = logging.getLogger("company_brain")

CompanyId = Annotated[
    str,
    Path(
        pattern=COMPANY_ID_RE.pattern,
        description="Tenant id. Each company has its own isolated FalkorDB graph.",
        examples=["northstar"],
    ),
]


def create_app(
    settings: Settings | None = None,
    store_factory: Callable[[Settings], GraphStore] = FalkorDBStore,
) -> FastAPI:
    settings = settings or get_settings()
    keys = ApiKeys.parse(settings.company_brain_api_keys.get_secret_value())
    app = FastAPI(
        title="Company Brain",
        version=__version__,
        description=(
            "Graph-powered organizational context for AI agents. Every answer is "
            "retrieved with fixed, parameterized Cypher queries against FalkorDB "
            "and points at its source records."
        ),
    )
    app.state.store = store_factory(settings)
    if not keys.enabled:
        log.warning("COMPANY_BRAIN_API_KEYS is empty: authentication is DISABLED")

    def store() -> GraphStore:
        s: GraphStore = app.state.store
        return s

    def authorize(
        company_id: CompanyId,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> str:
        if keys.enabled and not keys.allows(x_api_key, company_id):
            # Same response for unknown key and wrong company: do not reveal tenants.
            raise HTTPException(status_code=403, detail="not authorized for this company")
        return company_id

    Company = Annotated[str, Depends(authorize)]
    Store = Annotated[GraphStore, Depends(store)]

    # ------------------------------------------------------------ errors

    def _error(status: int, message: str) -> Callable[[Request, Exception], JSONResponse]:
        def handler(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status, content={"detail": message or str(exc)})

        return handler

    app.add_exception_handler(GraphUnavailable, _error(503, "graph database unavailable"))
    app.add_exception_handler(GraphTimeout, _error(504, "graph query timed out"))
    app.add_exception_handler(GraphConflict, _error(409, ""))
    app.add_exception_handler(CompanyNotFound, _error(404, "company has no graph yet"))
    app.add_exception_handler(EntityNotFound, _error(404, ""))
    app.add_exception_handler(GraphError, _error(500, "graph query failed"))

    def _bad_request(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.add_exception_handler(ValueError, _bad_request)

    # ------------------------------------------------------------ health

    @app.get("/health", tags=["health"], summary="Liveness: the HTTP process is running")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["health"], summary="Readiness: FalkorDB answers graph commands")
    def ready(s: Store) -> JSONResponse:
        try:
            s.ping()
        except GraphUnavailable:
            return JSONResponse(
                status_code=503, content={"status": "unavailable", "falkordb": "unreachable"}
            )
        return JSONResponse(content={"status": "ready", "falkordb": "ok"})

    @app.get("/v1/schema", tags=["schema"], summary="Allowed entity kinds and relationships")
    def schema() -> dict[str, Any]:
        return {
            "entity_kinds": {
                k.value: {"label": k.label, "properties": sorted(KIND_PROPERTIES[k])} for k in Kind
            },
            "relationships": {
                rel.value: {
                    "from": sorted(k.value for k in starts),
                    "to": sorted(k.value for k in ends),
                }
                for rel, (starts, ends) in RELATIONSHIP_RULES.items()
            },
        }

    # ------------------------------------------------------------ writes

    @app.post("/v1/companies/{company_id}/entities", tags=["ingest"], response_model=WriteResult)
    def upsert_entity(company_id: Company, body: EntityInput, s: Store) -> WriteResult:
        return Ingestor(s, company_id).upsert_entity(body)

    @app.post(
        "/v1/companies/{company_id}/relationships", tags=["ingest"], response_model=WriteResult
    )
    def upsert_relationship(company_id: Company, body: RelationshipInput, s: Store) -> WriteResult:
        return Ingestor(s, company_id).upsert_relationship(body)

    @app.post("/v1/companies/{company_id}/sources", tags=["ingest"], response_model=WriteResult)
    def register_source(company_id: Company, body: SourceInput, s: Store) -> WriteResult:
        return Ingestor(s, company_id).register_source(body)

    @app.post("/v1/companies/{company_id}/events", tags=["ingest"], response_model=EventResult)
    def apply_event(company_id: Company, body: Event, s: Store) -> EventResult:
        return Ingestor(s, company_id).apply_event(body)

    # ------------------------------------------------------------ reads

    @app.post(
        "/v1/companies/{company_id}/investigations",
        tags=["investigate"],
        response_model=InvestigationResponse,
        response_model_by_alias=True,
        summary="Retrieve connected, source-backed evidence for a business question",
    )
    def investigate(
        company_id: Company, body: InvestigationRequest, s: Store
    ) -> InvestigationResponse:
        return Investigator(s, company_id).investigate(body)

    @app.get(
        "/v1/companies/{company_id}/entities/{entity_id}",
        tags=["lookup"],
        response_model=EntityView,
    )
    def get_entity(company_id: Company, entity_id: str, s: Store) -> EntityView:
        return Lookup(s, company_id).entity(entity_id)

    @app.get(
        "/v1/companies/{company_id}/decisions/{decision_id}/sources",
        tags=["lookup"],
        response_model=DecisionProvenance,
    )
    def decision_sources(company_id: Company, decision_id: str, s: Store) -> DecisionProvenance:
        return Lookup(s, company_id).decision_sources(decision_id)

    @app.get(
        "/v1/companies/{company_id}/connections",
        tags=["lookup"],
        response_model=list[Connection],
        summary="Bounded paths between two entities (max depth 4)",
    )
    def connections(
        company_id: Company,
        s: Store,
        from_id: Annotated[str, Query(alias="from")],
        to_id: Annotated[str, Query(alias="to")],
        max_depth: Annotated[int, Query(ge=1, le=4)] = 3,
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> list[Connection]:
        return Lookup(s, company_id).connections(from_id, to_id, max_depth, limit)

    return app


def app() -> FastAPI:
    """Factory for ``uvicorn --factory company_brain.api:app``."""
    return create_app()
