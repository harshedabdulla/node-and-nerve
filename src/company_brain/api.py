"""HTTP API. Thin: validation and auth here, graph work in the services."""

import hmac
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from company_brain import __version__
from company_brain import queries as q
from company_brain.auth import ApiKeys
from company_brain.cashflow import CashflowInvestigator, CashflowRequest, CashflowResponse
from company_brain.config import Settings, get_settings
from company_brain.contract_cost import (
    ContractCostInvestigator,
    ContractCostRequest,
    ContractCostResponse,
    OverviewResponse,
    UnallocatedResponse,
)
from company_brain.csv_import import FILES, MAX_FILE_CHARS, CsvImporter, ImportReport
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
    iso,
)
from company_brain.schema import COMPANY_ID_RE, KIND_PROPERTIES, RELATIONSHIP_RULES, Kind
from company_brain.seed import CONTRACTS_AS_OF
from company_brain.tables import TaskTableImport, import_task_table
from company_brain.telegram_bot import Bot, Call, build_bot, telegram_call
from company_brain.trace import render_trace_page, verify

log = logging.getLogger("company_brain")

CompanyId = Annotated[
    str,
    Path(
        pattern=COMPANY_ID_RE.pattern,
        description="Tenant id. Each company has its own isolated FalkorDB graph.",
        examples=["northstar"],
    ),
]


class GraphSummary(BaseModel):
    company_id: str
    nodes_by_kind: dict[str, int]
    relationships_by_type: dict[str, int]
    total_nodes: int
    total_relationships: int
    last_updated: str | None = Field(description="Newest record write, ISO timestamp.")


class CsvImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: dict[str, Annotated[str, Field(max_length=MAX_FILE_CHARS)]] = Field(
        min_length=1,
        max_length=len(FILES),
        description=f"CSV text by file name. Accepted names: {', '.join(FILES)}.",
        examples=[
            {"customers.csv": "customer_id,name,city\nCUST-001,Shree Ganesh Contractors,Pune\n"}
        ],
    )
    dry_run: bool = Field(default=False, description="Validate only; write nothing.")
    observed_at: datetime | None = Field(
        default=None,
        description="When these records were exported. Rows without updated_at use it. "
        "Older versions never overwrite newer ones. Defaults to now.",
    )


def create_app(
    settings: Settings | None = None,
    store_factory: Callable[[Settings], GraphStore] = FalkorDBStore,
    telegram_call_factory: Callable[[str], Call] = telegram_call,
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

    @app.post(
        "/v1/companies/{company_id}/imports/tasks",
        tags=["ingest"],
        summary="Import a task table (sheet rows plus a column mapping) as tickets, owners, "
        "projects and blockers, each row kept as a source",
    )
    def import_tasks(company_id: Company, body: TaskTableImport, s: Store) -> dict[str, Any]:
        try:
            report = import_task_table(s, company_id, body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(report)

    @app.post(
        "/v1/companies/{company_id}/cashflow/imports",
        tags=["cashflow"],
        response_model=ImportReport,
        summary="Validate and import business records from CSV (all-or-nothing validation)",
        responses={
            422: {"model": ImportReport, "description": "Validation failed; nothing written"}
        },
    )
    def cashflow_import(company_id: Company, body: CsvImportRequest, s: Store) -> Any:
        report = CsvImporter(s, company_id).run(
            body.files, dry_run=body.dry_run, observed_at=body.observed_at, source_system="api"
        )
        if not report.valid:
            return JSONResponse(status_code=422, content=report.model_dump(mode="json"))
        return report

    @app.post(
        "/v1/companies/{company_id}/cashflow/investigations",
        tags=["cashflow"],
        response_model=CashflowResponse,
        response_model_by_alias=True,
        summary="Who owes money, and which commitments have a documented dependency on it",
    )
    def cashflow_investigate(
        company_id: Company, body: CashflowRequest, s: Store
    ) -> CashflowResponse:
        return CashflowInvestigator(s, company_id).investigate(body)

    @app.post(
        "/v1/companies/{company_id}/contracts/investigations",
        tags=["contracts"],
        response_model=ContractCostResponse,
        response_model_by_alias=True,
        summary="Recorded cost, provisional profit and missing information for a contract",
    )
    def contract_investigate(
        company_id: Company, body: ContractCostRequest, s: Store
    ) -> ContractCostResponse:
        return ContractCostInvestigator(s, company_id).investigate(body)

    @app.get(
        "/v1/companies/{company_id}/contracts",
        tags=["contracts"],
        response_model=OverviewResponse,
        summary="All contracts with provisional figures and which have incomplete costs",
    )
    def contracts_overview(company_id: Company, s: Store) -> OverviewResponse:
        return ContractCostInvestigator(s, company_id).overview()

    @app.get(
        "/v1/companies/{company_id}/costs/unallocated",
        tags=["contracts"],
        response_model=UnallocatedResponse,
        summary="Recorded costs not charged to any contract, and unapplied supplier payments",
    )
    def costs_unallocated(company_id: Company, s: Store) -> UnallocatedResponse:
        return ContractCostInvestigator(s, company_id).unallocated()

    @app.get(
        "/v1/companies/{company_id}/costs/changes",
        tags=["contracts"],
        summary="Cost records, payments and allocations added or changed since a timestamp",
    )
    def costs_changed(
        company_id: Company,
        s: Store,
        since: Annotated[datetime, Query(description="ISO timestamp, e.g. 2026-10-15T18:00:00Z")],
    ) -> list[dict[str, Any]]:
        return ContractCostInvestigator(s, company_id).changed_since(iso(since))

    @app.get(
        "/trace/{company_id}/contracts/{contract}",
        tags=["contracts"],
        response_class=HTMLResponse,
        summary="Signed, expiring page showing the evidence graph behind a contract investigation",
    )
    def contract_trace(
        company_id: CompanyId,
        contract: str,
        s: Store,
        exp: int,
        sig: str,
        as_of: date | None = None,
        focus: str = "",
    ) -> HTMLResponse:
        secret = settings.trace_link_secret.get_secret_value()
        if not verify(secret, company_id, contract, exp, sig):
            raise HTTPException(status_code=403, detail="trace link is invalid or has expired")
        response = ContractCostInvestigator(s, company_id).investigate(
            ContractCostRequest(contract=contract, as_of=as_of)
        )
        refs = [part for part in focus.split(",") if part][:50]
        return HTMLResponse(render_trace_page(s, company_id, response, refs))

    bots: list[Bot] = []

    @app.post("/telegram/webhook", include_in_schema=False)
    def telegram_webhook(
        update: dict[str, Any],
        s: Store,
        x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        """Webhook mode for serverless hosting: Telegram posts each update here."""
        secret = settings.telegram_webhook_secret.get_secret_value()
        token = settings.telegram_bot_token.get_secret_value()
        if not secret or not token:
            raise HTTPException(status_code=404, detail="Not Found")
        if not hmac.compare_digest(
            (x_telegram_bot_api_secret_token or "").encode(), secret.encode()
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        if not bots:
            bots.append(build_bot(settings, s, telegram_call_factory(token), CONTRACTS_AS_OF))
        try:
            bots[0].process_update(update)
        except RuntimeError as exc:  # a failed send; a retry by Telegram would answer twice
            log.warning("telegram webhook: %s", exc)
        return {"ok": True}

    @app.get(
        "/v1/companies/{company_id}/summary",
        tags=["lookup"],
        response_model=GraphSummary,
        summary="What the company graph holds: counts by kind and relationship type",
    )
    def graph_summary(company_id: Company, s: Store) -> GraphSummary:
        if not s.company_exists(company_id):
            raise CompanyNotFound(company_id)
        nodes = s.run(company_id, q.GRAPH_NODE_COUNTS)
        rels = s.run(company_id, q.GRAPH_RELATIONSHIP_COUNTS)
        stamps = [str(r["last_updated"]) for r in nodes if r["last_updated"]]
        return GraphSummary(
            company_id=company_id,
            nodes_by_kind={str(r["kind"]): int(r["count"]) for r in nodes},
            relationships_by_type={str(r["type"]): int(r["count"]) for r in rels},
            total_nodes=sum(int(r["count"]) for r in nodes),
            total_relationships=sum(int(r["count"]) for r in rels),
            last_updated=max(stamps) if stamps else None,
        )

    @app.get(
        "/v1/companies/{company_id}/attention",
        tags=["investigations"],
        summary="What needs attention: the investigation run for each project in the graph",
    )
    def attention(
        company_id: Company,
        s: Store,
        as_of: Annotated[
            date | None, Query(description="Reporting date. Defaults to today.")
        ] = None,
    ) -> dict[str, Any]:
        if not s.company_exists(company_id):
            raise CompanyNotFound(company_id)
        projects = s.run(company_id, q.GRAPH_PROJECTS, {"limit": 10})
        investigator = Investigator(s, company_id)
        results = [
            investigator.investigate(
                InvestigationRequest(
                    question=f"What needs attention in {p['name']}?",
                    focus_ids=[p["id"]],
                    as_of=as_of,
                )
            )
            for p in projects
        ]
        return {
            "company_id": company_id,
            "as_of": (as_of or date.today()).isoformat(),
            "projects": [r.model_dump(mode="json") for r in results],
            "project_limit": 10,
        }

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
