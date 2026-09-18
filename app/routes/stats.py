"""Rotas /v1/stats, sempre com Authorization: Bearer <NUNA_ADMIN_TOKEN>.

Nenhuma devolve session_id ou event_id. Todas são agregados, menos
/paywall/sessions: lançamentos anônimos do app, sem id, até 50 por vez.
"""

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import db, stats
from ..auth import require_admin
from ..catalog import Catalog
from ..config import Settings
from ..schemas import (
    BooksResponse,
    EventCountsResponse,
    FunnelResponse,
    OverviewResponse,
    PaywallConversionResponse,
    PaywallResponse,
    PaywallSessionsResponse,
)

router = APIRouter(prefix="/v1/stats", tags=["stats"], dependencies=[Depends(require_admin)])

FROM_QUERY = Query(None, alias="from", description="Dia inicial UTC, YYYY-MM-DD (padrão: 29 dias antes de to).")
TO_QUERY = Query(None, description="Dia final UTC inclusivo, YYYY-MM-DD (padrão: hoje).")


def _run(request: Request, raw_from: Optional[str], raw_to: Optional[str], query: Callable[..., Any]) -> Any:
    settings: Settings = request.app.state.settings
    try:
        rng = stats.resolve_range(raw_from, raw_to, db.utc_now().date())
        with db.connect(settings.db_path) as conn:
            return query(conn, rng)
    except stats.StatsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _catalog(request: Request) -> Catalog:
    return request.app.state.catalog


@router.get("/overview", response_model=OverviewResponse, summary="Eventos e sessões por dia, eventos mais frequentes")
def overview(request: Request, raw_from: Optional[str] = FROM_QUERY, to: Optional[str] = TO_QUERY) -> Any:
    return _run(request, raw_from, to, stats.overview)


@router.get("/events", response_model=EventCountsResponse, summary="Contagem de um evento por dia ou no total")
def events(
    request: Request,
    name: Optional[str] = Query(None, description="Nome do evento no catálogo."),
    group: str = Query("day", description="day ou total."),
    by: Optional[str] = Query(
        None,
        description="Propriedade do evento ou campo de contexto (app_version, build, os_version,"
        " device_family, layout, subscription_state) para quebrar a contagem.",
    ),
    raw_from: Optional[str] = FROM_QUERY,
    to: Optional[str] = TO_QUERY,
) -> Any:
    catalog = _catalog(request)
    return _run(request, raw_from, to, lambda conn, rng: stats.event_counts(conn, catalog, rng, name, group, by))


@router.get("/funnel", response_model=FunnelResponse, summary="Sessões que cumpriram os passos em ordem")
def funnel(
    request: Request,
    steps: Optional[str] = Query(
        None,
        description="2 a 10 passos separados por vírgula; filtros opcionais com"
        " nome:prop=valor, ex. paywall_viewed:source=locked_book,subscribe_tapped",
    ),
    raw_from: Optional[str] = FROM_QUERY,
    to: Optional[str] = TO_QUERY,
) -> Any:
    catalog = _catalog(request)
    return _run(request, raw_from, to, lambda conn, rng: stats.funnel(conn, catalog, rng, steps))


@router.get("/books", response_model=BooksResponse, summary="Aberturas, conclusões e taxa de conclusão por livro")
def books(request: Request, raw_from: Optional[str] = FROM_QUERY, to: Optional[str] = TO_QUERY) -> Any:
    return _run(request, raw_from, to, stats.books)


@router.get("/paywall", response_model=PaywallResponse, summary="Paywall, portão dos pais e compras")
def paywall(request: Request, raw_from: Optional[str] = FROM_QUERY, to: Optional[str] = TO_QUERY) -> Any:
    catalog = _catalog(request)
    return _run(request, raw_from, to, lambda conn, rng: stats.paywall(conn, catalog, rng))


@router.get(
    "/paywall/conversion",
    response_model=PaywallConversionResponse,
    summary="Funil de compra por sessão, no total e por segmento",
)
def paywall_conversion(
    request: Request,
    by: Optional[str] = Query(
        None,
        description="Segmento da primeira visualização: storefront, source, trial_eligible,"
        " install_age_bucket, prior_paywall_views_bucket, books_completed_bucket,"
        " device_family ou app_version.",
    ),
    raw_from: Optional[str] = FROM_QUERY,
    to: Optional[str] = TO_QUERY,
) -> Any:
    catalog = _catalog(request)
    return _run(request, raw_from, to, lambda conn, rng: stats.paywall_conversion(conn, catalog, rng, by))


@router.get(
    "/paywall/sessions",
    response_model=PaywallSessionsResponse,
    summary="Sessões anônimas que viram o paywall, com os próprios eventos, mais recentes primeiro",
)
def paywall_sessions(
    request: Request,
    outcome: Optional[str] = Query(None, description="all (padrão), purchased ou not_purchased."),
    limit: Optional[int] = Query(None, description="1 a 50 (padrão: 50)."),
    raw_from: Optional[str] = FROM_QUERY,
    to: Optional[str] = TO_QUERY,
) -> Any:
    catalog = _catalog(request)
    return _run(
        request,
        raw_from,
        to,
        lambda conn, rng: stats.paywall_session_list(conn, catalog, rng, outcome, limit),
    )
