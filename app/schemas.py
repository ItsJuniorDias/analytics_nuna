"""Modelos pydantic v2 de requisição e resposta.

typing.Optional/List/Union em vez de `X | None`: o código precisa rodar no
Python 3.9 da máquina local, onde o pydantic avalia anotações em tempo de
execução e `X | None` quebra.

O envelope de upload é validado aqui; o contexto do lote e cada evento são
validados contra o catálogo em app/catalog.py, porque precisamos rejeitar
evento por evento, com motivo, sem derrubar o lote inteiro.
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# Ingestão ------------------------------------------------------------------


class IngestEnvelope(_Strict):
    """Corpo do POST no formato do app (CorpoDoEnvio em Analytics.swift)."""

    # Dict[str, Any]: o formato dos campos é conferido contra o catálogo, que
    # devolve motivo ("pattern_mismatch: context.app_version") em vez de 422.
    context: Dict[str, Any]
    # List[Any]: um item que não é objeto vira rejeição daquele índice
    # ("event_not_object"), não 422 do lote todo.
    events: List[Any]
    # O app atual não manda: ausente vale a versão 1, a que ele implementa.
    # Existe para um app futuro declarar outro catálogo.
    catalog_version: Optional[int] = None


class RejectedEvent(BaseModel):
    index: int = Field(description="Posição do evento no array enviado (base 0).")
    reason: str = Field(description='"codigo: caminho", ex. "invalid_enum: properties.source".')


class IngestResult(BaseModel):
    accepted: int = Field(description="Eventos novos gravados.")
    duplicates: int = Field(description="event_id já gravado antes; tratado como sucesso.")
    rejected: List[RejectedEvent]


class ErrorResponse(BaseModel):
    detail: str


class Health(BaseModel):
    status: str
    catalog_version: int


# Estatísticas --------------------------------------------------------------

Scalar = Union[bool, int, str, None]


class DayCount(BaseModel):
    day: str
    events: int
    sessions: int


class NameCount(BaseModel):
    name: str
    count: int


class OverviewTotals(BaseModel):
    events: int
    sessions: int


class OverviewResponse(BaseModel):
    from_: str = Field(alias="from")
    to: str
    totals: OverviewTotals
    days: List[DayCount]
    top_events: List[NameCount]

    model_config = ConfigDict(populate_by_name=True)


class EventCountRow(BaseModel):
    day: Optional[str] = None
    value: Scalar = None
    count: int


class EventCountsResponse(BaseModel):
    name: str
    from_: str = Field(alias="from")
    to: str
    group: str
    by: Optional[str] = None
    total: int
    rows: List[EventCountRow]

    model_config = ConfigDict(populate_by_name=True)


class FunnelStep(BaseModel):
    step: str
    name: str
    filters: Dict[str, Union[bool, int, str]]
    sessions: int
    conversion_from_previous: Optional[float] = None
    conversion_from_first: Optional[float] = None


class FunnelResponse(BaseModel):
    from_: str = Field(alias="from")
    to: str
    steps: List[FunnelStep]

    model_config = ConfigDict(populate_by_name=True)


class BookRow(BaseModel):
    book_id: str
    opens: int
    sessions: int
    completions: int
    completion_rate: Optional[float] = None


class BooksResponse(BaseModel):
    from_: str = Field(alias="from")
    to: str
    books: List[BookRow]

    model_config = ConfigDict(populate_by_name=True)


class ValueCount(BaseModel):
    value: Scalar = None
    count: int


class GateRow(BaseModel):
    purpose: str
    shown: int
    passed: int
    failed: int
    cancelled: int
    pass_rate: Optional[float] = None


class PurchaseRow(BaseModel):
    plan: str
    result: str
    count: int


class PaywallResponse(BaseModel):
    from_: str = Field(alias="from")
    to: str
    views_total: int
    views_by_source: List[ValueCount]
    subscribe_taps_total: int
    subscribe_taps_by_plan: List[ValueCount]
    gate: List[GateRow]
    purchases: List[PurchaseRow]
    purchases_completed: int
    view_to_purchase_rate: Optional[float] = None
    closes_by_reason: List[ValueCount]
    restores_by_result: List[ValueCount]

    model_config = ConfigDict(populate_by_name=True)
