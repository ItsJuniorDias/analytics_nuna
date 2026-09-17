"""Rotas /v1/stats: só agregados, sempre com Authorization: Bearer <NUNA_ADMIN_TOKEN>."""

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
    PaywallResponse,
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
