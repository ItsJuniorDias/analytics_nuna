"""Aplicação FastAPI.

    uvicorn app.main:app --no-access-log --no-proxy-headers

Sempre com --no-access-log: o access log do uvicorn grava o IP de cada
requisição, e a política promete não guardar IP. Ver app/privacy.py.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, db
from .catalog import Catalog
from .config import Settings
from .privacy import PrivacyScrubMiddleware, SecurityHeadersMiddleware
from .purge import PurgeScheduler
from .routes import dashboard, ingest, stats

logger = logging.getLogger("nuna.analytics")


def _configure_logging() -> None:
    # O uvicorn só configura os loggers dele; sem isto, os avisos do app iriam
    # para o handler de último recurso e o log da limpeza sumiria. O formato
    # não tem campo de cliente: não existe IP para logar (ver privacy.py).
    if logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def create_app(settings: Optional[Settings] = None, catalog: Optional[Catalog] = None) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    # O catálogo é carregado e conferido já na criação: um events.json inválido
    # derruba a subida em vez de aceitar eventos sem validação.
    catalog = catalog if catalog is not None else Catalog.load(settings.catalog_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        db.ensure_db(settings.db_path)
        if not settings.app_keys:
            logger.warning("NUNA_APP_KEYS is empty: every POST /v1/events will get 401")
        if settings.admin_token is None:
            logger.warning("NUNA_ADMIN_TOKEN is empty: /v1/stats is disabled (401)")
        scheduler = None
        if settings.purge_interval_hours > 0:
            scheduler = PurgeScheduler(settings.db_path, settings.retention_days, settings.purge_interval_hours)
            scheduler.start()
        app.state.purge_scheduler = scheduler
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()

    app = FastAPI(
        title="Nuna Analytics",
        version=__version__,
        description="Analytics interno e anônimo do app Nuna (catálogo v%d)." % catalog.version,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.purge_scheduler = None

    app.include_router(ingest.router)
    app.include_router(stats.router)
    app.include_router(dashboard.router)

    if settings.cors_origins:
        # O app iOS não precisa de CORS; isto só existe para um painel
        # hospedado em outro domínio. Sem cookies, então sem credentials.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Nuna-Key"],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(SecurityHeadersMiddleware)
    # Adicionado por último = middleware mais externo: nenhuma camada abaixo
    # (CORS, rotas, handlers de erro) chega a ver IP ou User-Agent.
    app.add_middleware(PrivacyScrubMiddleware)
    return app


app = create_app()
