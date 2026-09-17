"""POST /v1/events, GET /health e GET /v1/catalog."""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, List, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from .. import db
from ..auth import require_app_key
from ..catalog import Catalog, EventRejected, ValidEvent
from ..config import Settings
from ..schemas import ErrorResponse, Health, IngestEnvelope, IngestResult, RejectedEvent

logger = logging.getLogger("nuna.analytics.ingest")

router = APIRouter()

# Versão do catálogo quando o corpo não declara uma: a do app que existe hoje
# (AnalyticsEvent.swift espelha o events.json v1 e não manda o campo). Não é
# "a versão carregada no servidor": com um catálogo v2 no ar, um app antigo
# sem o campo continua sendo v1 e recebe 422 em vez de validação errada.
IMPLICIT_CATALOG_VERSION = 1

# Nenhum inteiro do catálogo passa de 5 dígitos. No Python 3.9 (sem o limite de
# dígitos do 3.11), converter um inteiro de 250 mil dígitos leva meio segundo
# de CPU, parado no event loop.
MAX_INTEGER_DIGITS = 32

INSERT_SQL = (
    "INSERT INTO events (event_id, name, session_id, ts, received_at, catalog_version,"
    " app_version, build, os_version, device_family, layout, subscription_state, properties)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    # Reenvio do mesmo event_id (resposta perdida, app morto antes de limpar a
    # fila) não é erro: o primeiro gravado vale e o repetido conta como duplicado.
    " ON CONFLICT (event_id) DO NOTHING"
)


async def _read_limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(status_code=413, detail="body_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_content_length")
    # Conta os bytes de verdade também: sem Content-Length (chunked) ou com um
    # valor mentiroso, o limite continua valendo.
    size = 0
    chunks: List[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_constant(_: str) -> Any:
    # json.loads aceita NaN e Infinity por padrão; JSON de verdade não.
    raise ValueError("non-standard JSON constant")


def _no_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> dict:
    # Com chave repetida, json.loads fica com a última em silêncio; o que foi
    # validado poderia não ser o que o cliente pretendia mandar.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_int(text: str) -> int:
    if len(text) > MAX_INTEGER_DIGITS + 1:  # + 1 para o sinal
        raise ValueError("integer literal too long")
    return int(text)


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_int=_parse_int,
            object_pairs_hook=_no_duplicate_keys,
        )
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="invalid_json")


def store_events(db_path: str, events: Sequence[ValidEvent], catalog_version: int, now: datetime) -> Tuple[int, int]:
    if not events:
        return 0, 0
    # Só o dia (UTC), de propósito: um upload pode trazer várias sessões do
    # mesmo aparelho, e um horário exato de chegada igual para todas elas
    # permitiria ligar essas sessões entre si. O dia basta para a retenção.
    received_day = now.strftime("%Y-%m-%d")
    accepted = 0
    duplicates = 0
    with db.connect(db_path) as conn, db.write_transaction(conn):
        for e in events:
            cursor = conn.execute(
                INSERT_SQL,
                (
                    e.event_id,
                    e.name,
                    e.session_id,
                    e.ts,
                    received_day,
                    catalog_version,
                    e.app_version,
                    e.build,
                    e.os_version,
                    e.device_family,
                    e.layout,
                    e.subscription_state,
                    e.properties_json(),
                ),
            )
            if cursor.rowcount == 1:
                accepted += 1
            else:
                duplicates += 1
    return accepted, duplicates


@router.post(
    "/v1/events",
    response_model=IngestResult,
    summary="Recebe um lote de eventos do app",
    responses={
        400: {"model": ErrorResponse, "description": "invalid_json / invalid_content_length"},
        401: {"model": ErrorResponse, "description": "invalid_app_key"},
        413: {"model": ErrorResponse, "description": "body_too_large / batch_too_large"},
        415: {"model": ErrorResponse, "description": "unsupported_media_type"},
        422: {"model": ErrorResponse, "description": "invalid_envelope / unsupported_catalog_version"},
    },
    dependencies=[Depends(require_app_key)],
)
async def ingest_events(request: Request) -> IngestResult:
    settings: Settings = request.app.state.settings
    catalog: Catalog = request.app.state.catalog

    media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="unsupported_media_type")

    body = await _read_limited_body(request, settings.max_body_bytes)
    payload = _parse_json(body)
    try:
        envelope = IngestEnvelope.model_validate(payload)
    except ValidationError:
        # Sem detalhes do pydantic: eles ecoam o valor recebido.
        raise HTTPException(status_code=422, detail="invalid_envelope")
    version = IMPLICIT_CATALOG_VERSION if envelope.catalog_version is None else envelope.catalog_version
    if version != catalog.version:
        raise HTTPException(status_code=422, detail="unsupported_catalog_version")
    if len(envelope.events) > settings.max_batch:
        raise HTTPException(status_code=413, detail="batch_too_large")

    now = db.utc_now()
    max_age = timedelta(days=settings.max_event_age_days)
    max_skew = timedelta(hours=settings.max_clock_skew_hours)
    valid: List[ValidEvent] = []
    rejected: List[RejectedEvent] = []
    try:
        context = catalog.validate_batch_context(envelope.context)
        context_error = None
    except EventRejected as exc:
        # 200 com todos recusados, e não 422: mantém a conta
        # accepted + duplicates + rejected == len(events) e devolve o motivo.
        context = {}
        context_error = exc.reason
    for index, raw_event in enumerate(envelope.events):
        if context_error is not None:
            rejected.append(RejectedEvent(index=index, reason=context_error))
            continue
        try:
            valid.append(catalog.validate_event(raw_event, context, now, max_age, max_skew))
        except EventRejected as exc:
            rejected.append(RejectedEvent(index=index, reason=exc.reason))

    accepted, duplicates = await run_in_threadpool(
        store_events, settings.db_path, valid, catalog.version, now
    )

    if rejected:
        # Só os códigos, sem caminho nem valor: log não é lugar de dado do cliente.
        codes = Counter(r.reason.split(":", 1)[0] for r in rejected)
        logger.warning("rejected events by code: %s", dict(sorted(codes.items())))
    logger.debug("batch accepted=%d duplicates=%d rejected=%d", accepted, duplicates, len(rejected))
    return IngestResult(accepted=accepted, duplicates=duplicates, rejected=rejected)


@router.get("/health", response_model=Health, summary="Verificação de saúde (inclui o banco)")
def health(request: Request) -> Health:
    settings: Settings = request.app.state.settings
    catalog: Catalog = request.app.state.catalog
    try:
        with db.connect(settings.db_path) as conn:
            conn.execute("SELECT 1 FROM events LIMIT 1").fetchall()
    except Exception:
        logger.exception("health check: database unavailable")
        raise HTTPException(status_code=503, detail="database_unavailable")
    return Health(status="ok", catalog_version=catalog.version)


@router.get("/v1/catalog", summary="Catálogo de eventos aceito por este servidor")
def get_catalog(request: Request) -> Any:
    # Público: é o mesmo contrato que já vai dentro do app, sem segredo nenhum.
    return request.app.state.catalog.raw
