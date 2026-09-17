"""Fábricas de eventos no formato que o app envia e atalhos de requisição.

O corpo do POST é o de Nuna/Services/Analytics.swift (CorpoDoEnvio):

    {"context": {session_id, app_version, build, os_version, device_family},
     "events": [{event_id, name, timestamp, properties, layout?, subscription_state}]}

make_event devolve um evento já nesse formato. Campos do contexto do LOTE
passados a make_event (session_id=..., device_family=...) ficam guardados na
chave interna "_lote"; post_batch tira essa chave e, como o app, manda um POST
por contexto diferente, somando as respostas como se fosse um só.
"""

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi.testclient import TestClient

from app.catalog import BATCH_CONTEXT_FIELDS, Catalog, format_timestamp

APP_KEY = "test-app-key-1"
APP_KEY_ROTATED = "test-app-key-2"
ADMIN_TOKEN = "admin-token-for-tests-0123456789"

# Chave interna com os campos de contexto do lote de um evento. Nunca vai
# para o servidor.
LOTE = "_lote"
# Valor em "_lote" que tira o campo do contexto (testar campo obrigatório).
APAGAR = object()

# Exemplo válido para cada padrão de string usado em propriedades de evento.
PATTERN_SAMPLES = {
    "^[a-z0-9-]{1,64}$": "o-quintal-da-nuna",
}


def new_uuid() -> str:
    return str(uuid.uuid4())


def now_ts(offset: timedelta = timedelta(0)) -> str:
    return format_timestamp(datetime.now(timezone.utc) + offset)


def make_batch_context(**overrides: Any) -> Dict[str, Any]:
    context = {
        "session_id": new_uuid(),
        "app_version": "1.0.2",
        "build": "42",
        "os_version": "27.0",
        "device_family": "phone",
    }
    context.update(overrides)
    return context


def sample_properties(catalog: Catalog, name: str, include_optional: bool = True) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for prop, spec in catalog.events[name].properties.items():
        if not spec.required and not include_optional:
            continue
        if spec.type == "boolean":
            props[prop] = True
        elif spec.type == "integer":
            props[prop] = spec.minimum
        elif spec.enum is not None:
            props[prop] = spec.enum[0]
        else:
            props[prop] = PATTERN_SAMPLES[spec.pattern.pattern]
    return props


def make_event(
    catalog: Catalog,
    name: str,
    properties: Optional[Dict[str, Any]] = None,
    **fields: Any
) -> Dict[str, Any]:
    """Evento como o app manda. `fields` aceita os campos do evento
    (event_id, timestamp, layout, subscription_state) e os do contexto do lote
    (session_id, app_version, build, os_version, device_family)."""
    event: Dict[str, Any] = {
        "event_id": new_uuid(),
        "name": name,
        "timestamp": now_ts(),
        "properties": copy.deepcopy(properties) if properties is not None else sample_properties(catalog, name),
        "subscription_state": "free",
    }
    lote: Dict[str, Any] = {}
    for key, value in fields.items():
        if key in BATCH_CONTEXT_FIELDS:
            lote[key] = value
        else:
            event[key] = value
    if lote:
        event[LOTE] = lote
    return event


def wire(event: Any) -> Any:
    """O evento exatamente como sai no JSON, sem a chave interna."""
    if isinstance(event, dict):
        return {k: v for k, v in event.items() if k != LOTE}
    return event


def _split_batches(events: List[Any]) -> List[Tuple[Dict[str, Any], List[Tuple[int, Any]]]]:
    """Agrupa como o app: eventos com o mesmo contexto vão no mesmo lote, na
    ordem em que aparecem. Quem não traz "_lote" usa o contexto padrão da
    chamada, então uma lista comum vira um POST só."""
    default = make_batch_context()
    batches: Dict[str, Tuple[Dict[str, Any], List[Tuple[int, Any]]]] = {}
    for index, event in enumerate(events):
        context = dict(default)
        if isinstance(event, dict):
            for key, value in event.get(LOTE, {}).items():
                if value is APAGAR:
                    context.pop(key, None)
                else:
                    context[key] = value
        key = json.dumps(context, sort_keys=True, default=repr)
        if key not in batches:
            batches[key] = (context, [])
        batches[key][1].append((index, wire(event)))
    if not batches:
        return [(default, [])]
    return list(batches.values())


def batch_body(events: List[Any], **context: Any) -> Dict[str, Any]:
    """Corpo de um lote só (para testes que postam bytes à mão)."""
    return {"context": make_batch_context(**context), "events": [wire(e) for e in events]}


class CombinedResponse:
    """Respostas de vários POSTs (um por contexto) somadas como uma só, com os
    índices de rejeição de volta às posições da lista original."""

    def __init__(self, parts: List[Tuple[Any, List[int]]]) -> None:
        self._parts = parts
        failed = [r for r, _ in parts if r.status_code != 200]
        self._failed = failed[0] if failed else None
        self.status_code = self._failed.status_code if self._failed else 200

    def json(self) -> Dict[str, Any]:
        if self._failed is not None:
            return self._failed.json()
        accepted = 0
        duplicates = 0
        rejected: List[Dict[str, Any]] = []
        for response, indexes in self._parts:
            data = response.json()
            accepted += data["accepted"]
            duplicates += data["duplicates"]
            for item in data["rejected"]:
                rejected.append({"index": indexes[item["index"]], "reason": item["reason"]})
        rejected.sort(key=lambda item: item["index"])
        return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}

    @property
    def text(self) -> str:
        return "\n".join(r.text for r, _ in self._parts)


def post_batch(client: TestClient, events: List[Any], key: Optional[str] = APP_KEY, **kwargs: Any):
    headers = kwargs.pop("headers", {})
    if key is not None:
        headers["X-Nuna-Key"] = key
    body = kwargs.pop("body", None)
    if body is not None:
        return client.post("/v1/events", json=body, headers=headers, **kwargs)

    parts = []
    for context, items in _split_batches(events):
        payload = {"context": context, "events": [event for _, event in items]}
        response = client.post("/v1/events", json=payload, headers=dict(headers), **kwargs)
        parts.append((response, [index for index, _ in items]))
    if len(parts) == 1:
        return parts[0][0]
    return CombinedResponse(parts)


def admin_headers(token: str = ADMIN_TOKEN) -> Dict[str, str]:
    return {"Authorization": "Bearer " + token}
