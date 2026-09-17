"""Catálogo de eventos (catalog/events.json) e validação estrita.

O catálogo é a única fonte da verdade do que pode ser gravado. Qualquer campo
fora dele é rejeitado, e não descartado em silêncio: descartar esconderia um bug
do cliente, e aceitar abriria caminho para texto livre (nome, busca, mensagem de
erro) chegar ao banco de um app infantil.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Pattern, Tuple, Union

# Códigos de rejeição. O formato devolvido ao cliente é "codigo: caminho", por
# exemplo "invalid_enum: properties.source". O valor recebido nunca é ecoado:
# se fosse texto livre por engano, voltaria em respostas e logs.
EVENT_NOT_OBJECT = "event_not_object"
UNKNOWN_FIELD = "unknown_field"
MISSING_FIELD = "missing_field"
UNKNOWN_EVENT = "unknown_event"
UNKNOWN_PROPERTY = "unknown_property"
MISSING_REQUIRED = "missing_required"
NULL_VALUE = "null_value"
INVALID_TYPE = "invalid_type"
INVALID_ENUM = "invalid_enum"
PATTERN_MISMATCH = "pattern_mismatch"
OUT_OF_RANGE = "out_of_range"
INVALID_TIMESTAMP = "invalid_timestamp"
TIMESTAMP_OUT_OF_WINDOW = "timestamp_out_of_window"
STRING_TOO_LONG = "string_too_long"

# Formato que o app envia (Nuna/Services/Analytics.swift, CorpoDoEnvio):
#
#   {"context": {session_id, app_version, build, os_version, device_family, storefront?},
#    "events": [{event_id, name, timestamp, properties, layout?, subscription_state}]}
#
# O app só junta num lote eventos com o mesmo contexto, então o que não muda
# dentro da sessão vai uma vez por lote. layout e subscription_state mudam no
# meio da sessão (dobrar o Duo, comprar) e vão em cada evento.
BATCH_CONTEXT_FIELDS = ("session_id", "app_version", "build", "os_version", "device_family", "storefront")
EVENT_CONTEXT_FIELDS = ("event_id", "timestamp", "layout", "subscription_state")
EVENT_FIELDS = ("event_id", "name", "timestamp", "properties", "layout", "subscription_state")

# Colunas da tabela events que vêm do contexto (do lote ou do evento). Se o
# catálogo ganhar um campo de contexto novo, o carregamento falha até existir
# migração para ele.
CONTEXT_FIELDS = (
    "event_id",
    "session_id",
    "timestamp",
    "app_version",
    "build",
    "os_version",
    "device_family",
    "storefront",
    "layout",
    "subscription_state",
)

# Campos de contexto que podem faltar (coluna NULL): layout só existe no leitor,
# e o StoreKit nem sempre informa a loja (sem conta Apple, sem rede).
OPTIONAL_CONTEXT_FIELDS = ("layout", "storefront")

SUPPORTED_TYPES = ("string", "integer", "boolean")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SPEC_KEYS = {"type", "required", "enum", "pattern", "minimum", "maximum", "description"}
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
# [0-9] e não \d: em str, \d aceita dígitos de qualquer escrita ("٢٠٢٦").
_TIMESTAMP_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,3}))?Z")
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Nenhum valor legítimo passa de 64 caracteres (book_id). O teto vale mesmo se
# um padrão do catálogo ficar frouxo demais (ex. "^[a-z-]+$"), e poupa rodar
# regex em texto enorme.
MAX_STRING_LENGTH = 128

# strptime importa _strptime de forma preguiçosa, e a primeira chamada
# concorrente em threads diferentes pode falhar. Aquecer no import evita isso.
datetime.strptime("2000-01-01T00:00:00", _TIMESTAMP_FORMAT)

JSONScalar = Union[str, int, bool]


def _safe_key(key: str) -> str:
    """Nome de campo desconhecido vem do cliente: só volta na resposta se tiver
    cara de identificador, senão um nome de campo virava canal de texto livre."""
    return key if _SAFE_KEY_RE.fullmatch(key) else "<invalid_key>"


class CatalogError(ValueError):
    """O próprio events.json é inválido para este servidor."""


class EventRejected(Exception):
    def __init__(self, code: str, field: str) -> None:
        super().__init__("%s: %s" % (code, field))
        self.code = code
        self.field = field

    @property
    def reason(self) -> str:
        return "%s: %s" % (self.code, self.field)


@dataclass(frozen=True)
class PropertySpec:
    name: str
    type: str
    required: bool
    enum: Optional[Tuple[str, ...]] = None
    pattern: Optional[Pattern[str]] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    def check(self, value: Any, path: str) -> None:
        if value is None:
            raise EventRejected(NULL_VALUE, path)
        if self.type == "string":
            if not isinstance(value, str):
                raise EventRejected(INVALID_TYPE, path)
            if len(value) > MAX_STRING_LENGTH:
                raise EventRejected(STRING_TOO_LONG, path)
            if self.enum is not None and value not in self.enum:
                raise EventRejected(INVALID_ENUM, path)
            # fullmatch e não match: com re.match, "$" aceita um "\n" no fim, e
            # uma quebra de linha passaria pelo padrão do book_id.
            if self.pattern is not None and self.pattern.fullmatch(value) is None:
                raise EventRejected(PATTERN_MISMATCH, path)
            return
        if self.type == "integer":
            # bool é subclasse de int em Python; true não pode virar 1.
            # 3.0 (float) também não vale: o catálogo diz inteiro.
            if type(value) is not int:
                raise EventRejected(INVALID_TYPE, path)
            if (self.minimum is not None and value < self.minimum) or (
                self.maximum is not None and value > self.maximum
            ):
                raise EventRejected(OUT_OF_RANGE, path)
            return
        if self.type == "boolean":
            if type(value) is not bool:
                raise EventRejected(INVALID_TYPE, path)
            return
        raise EventRejected(INVALID_TYPE, path)  # inalcançável: tipos checados no load

    def parse_text(self, raw: str) -> JSONScalar:
        """Converte um valor vindo de query string (filtros do funil)."""
        if self.type == "boolean":
            if raw == "true":
                value: JSONScalar = True
            elif raw == "false":
                value = False
            else:
                raise ValueError("expected true or false")
        elif self.type == "integer":
            if not re.fullmatch(r"-?[0-9]{1,9}", raw):
                raise ValueError("expected integer")
            value = int(raw)
        else:
            value = raw
        try:
            self.check(value, self.name)
        except EventRejected as exc:
            raise ValueError(exc.code)
        return value


@dataclass(frozen=True)
class EventSpec:
    name: str
    description: str
    properties: Mapping[str, PropertySpec]


@dataclass(frozen=True)
class ValidEvent:
    event_id: str
    name: str
    session_id: str
    ts: str
    app_version: str
    build: str
    os_version: str
    device_family: str
    storefront: Optional[str]
    layout: Optional[str]
    subscription_state: str
    properties: Mapping[str, JSONScalar]

    def properties_json(self) -> str:
        return json.dumps(dict(self.properties), separators=(",", ":"), sort_keys=True)


def parse_timestamp(value: str) -> datetime:
    """ISO 8601 em UTC com "Z" (o ISO8601DateFormatter do app) -> datetime UTC.

    Confere o formato aqui mesmo, sem depender do padrão do catálogo: com
    fuso ("-03:00") ou sem "Z", tratar o texto como UTC gravaria outro instante.
    """
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must be UTC ISO 8601 ending in Z")
    # Data ou hora impossível (30 de fevereiro, 25h, segundo 60) vira ValueError.
    moment = datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    fraction = match.group(2)
    if fraction:
        # ".5" é meio segundo, não 5 ms: completa à direita.
        moment += timedelta(milliseconds=int((fraction + "00")[:3]))
    return moment


def format_timestamp(moment: datetime) -> str:
    """Sempre 3 casas de milissegundo: a ordem de texto vira a ordem temporal,
    e os filtros por período funcionam com comparação de string no índice."""
    moment = moment.astimezone(timezone.utc)
    return "%s.%03dZ" % (moment.strftime(_TIMESTAMP_FORMAT), moment.microsecond // 1000)


def _load_property(owner: str, name: str, raw: Any) -> PropertySpec:
    where = "%s.%s" % (owner, name)
    if not _NAME_RE.fullmatch(name):
        raise CatalogError("nome de propriedade inválido: %s" % where)
    if not isinstance(raw, dict):
        raise CatalogError("%s deve ser objeto" % where)
    unknown = set(raw) - _SPEC_KEYS
    if unknown:
        raise CatalogError("%s tem chaves desconhecidas: %s" % (where, sorted(unknown)))
    kind = raw.get("type")
    if kind not in SUPPORTED_TYPES:
        raise CatalogError("%s: tipo não suportado %r" % (where, kind))
    required = raw.get("required")
    if type(required) is not bool:
        raise CatalogError("%s: required deve ser boolean" % where)

    enum = None
    pattern = None
    minimum = None
    maximum = None
    if kind == "string":
        if "minimum" in raw or "maximum" in raw:
            raise CatalogError("%s: minimum/maximum só valem para integer" % where)
        if "enum" in raw:
            values = raw["enum"]
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(v, str) and v for v in values)
                or len(set(values)) != len(values)
            ):
                raise CatalogError("%s: enum deve ser lista não vazia de strings únicas" % where)
            enum = tuple(values)
        if "pattern" in raw:
            source = raw["pattern"]
            if not isinstance(source, str) or not source.startswith("^") or not source.endswith("$"):
                raise CatalogError("%s: pattern deve ser ancorado com ^ e $" % where)
            try:
                pattern = re.compile(source)
            except re.error as exc:
                raise CatalogError("%s: pattern inválido (%s)" % (where, exc))
        # Garantia de privacidade por construção: string sem enum nem padrão
        # seria texto livre.
        if enum is None and pattern is None:
            raise CatalogError("%s: string sem enum nem pattern (texto livre)" % where)
    elif kind == "integer":
        if "enum" in raw or "pattern" in raw:
            raise CatalogError("%s: enum/pattern não valem para integer" % where)
        minimum = raw.get("minimum")
        maximum = raw.get("maximum")
        if type(minimum) is not int or type(maximum) is not int or minimum > maximum:
            raise CatalogError("%s: integer precisa de minimum <= maximum" % where)
    else:
        if set(raw) - {"type", "required", "description"}:
            raise CatalogError("%s: boolean não aceita restrições" % where)

    return PropertySpec(
        name=name,
        type=kind,
        required=required,
        enum=enum,
        pattern=pattern,
        minimum=minimum,
        maximum=maximum,
    )


class Catalog:
    def __init__(
        self,
        version: int,
        context: Mapping[str, PropertySpec],
        events: Mapping[str, EventSpec],
        raw: Mapping[str, Any],
    ) -> None:
        self.version = version
        self.context = context
        self.events = events
        self.raw = raw
        # O mesmo contrato de contexto, repartido como o app envia.
        self.batch_context: Mapping[str, PropertySpec] = {k: context[k] for k in BATCH_CONTEXT_FIELDS}
        self.event_context: Mapping[str, PropertySpec] = {k: context[k] for k in EVENT_CONTEXT_FIELDS}

    # Carregamento -----------------------------------------------------------

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Catalog":
        with open(str(path), "r", encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except ValueError as exc:
                raise CatalogError("events.json não é JSON válido: %s" % exc)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "Catalog":
        if not isinstance(data, dict):
            raise CatalogError("o catálogo deve ser um objeto")
        unknown = set(data) - {"version", "context", "events"}
        if unknown:
            raise CatalogError("chaves desconhecidas no catálogo: %s" % sorted(unknown))
        version = data.get("version")
        if type(version) is not int or version < 1:
            raise CatalogError("version deve ser inteiro >= 1")

        raw_context = data.get("context")
        if not isinstance(raw_context, dict) or not raw_context:
            raise CatalogError("context deve ser objeto não vazio")
        extra = set(raw_context) - set(CONTEXT_FIELDS)
        missing = set(CONTEXT_FIELDS) - set(raw_context)
        if extra or missing:
            raise CatalogError(
                "context não bate com as colunas do banco (sobrando: %s, faltando: %s)"
                % (sorted(extra), sorted(missing))
            )
        context: Dict[str, PropertySpec] = {}
        for name, spec in raw_context.items():
            context[name] = _load_property("context", name, spec)
        for name in CONTEXT_FIELDS:
            if name not in OPTIONAL_CONTEXT_FIELDS and not context[name].required:
                raise CatalogError("context.%s precisa ser obrigatório (coluna NOT NULL)" % name)
            if name in ("event_id", "session_id", "timestamp") and context[name].pattern is None:
                raise CatalogError("context.%s precisa de pattern" % name)

        raw_events = data.get("events")
        if not isinstance(raw_events, dict) or not raw_events:
            raise CatalogError("events deve ser objeto não vazio")
        events: Dict[str, EventSpec] = {}
        for event_name, raw_event in raw_events.items():
            if not _NAME_RE.fullmatch(event_name):
                raise CatalogError("nome de evento inválido: %s" % event_name)
            if not isinstance(raw_event, dict):
                raise CatalogError("%s deve ser objeto" % event_name)
            if set(raw_event) - {"description", "properties"}:
                raise CatalogError("%s tem chaves desconhecidas" % event_name)
            description = raw_event.get("description", "")
            if not isinstance(description, str):
                raise CatalogError("%s.description deve ser string" % event_name)
            raw_props = raw_event.get("properties")
            if not isinstance(raw_props, dict):
                raise CatalogError("%s.properties deve ser objeto" % event_name)
            props: Dict[str, PropertySpec] = {}
            for prop_name, prop_spec in raw_props.items():
                props[prop_name] = _load_property(event_name, prop_name, prop_spec)
            events[event_name] = EventSpec(event_name, description, props)

        return cls(version=version, context=context, events=events, raw=data)

    # Consulta ---------------------------------------------------------------

    @property
    def event_names(self) -> List[str]:
        return list(self.events)

    def has_event(self, name: str) -> bool:
        return name in self.events

    def property_spec(self, event_name: str, prop: str) -> Optional[PropertySpec]:
        event = self.events.get(event_name)
        if event is None:
            return None
        return event.properties.get(prop)

    # Validação --------------------------------------------------------------

    @staticmethod
    def _check_object(
        specs: Mapping[str, PropertySpec], values: Mapping[str, Any], prefix: Optional[str]
    ) -> Dict[str, JSONScalar]:
        def path_of(key: str) -> str:
            return key if prefix is None else "%s.%s" % (prefix, key)

        for key in sorted(values):
            if key not in specs:
                raise EventRejected(UNKNOWN_PROPERTY, path_of(_safe_key(key)))
        clean: Dict[str, JSONScalar] = {}
        for name, spec in specs.items():
            path = path_of(name)
            if name not in values:
                if spec.required:
                    raise EventRejected(MISSING_REQUIRED, path)
                continue
            value = values[name]
            spec.check(value, path)
            clean[name] = value
        return clean

    def validate_batch_context(self, context: Any) -> Dict[str, JSONScalar]:
        """Valida o "context" do lote; lança EventRejected com o primeiro erro.

        O erro vale para todos os eventos do lote: nenhum deles tem sessão ou
        versão confiável sem um contexto válido.
        """
        if context is None:
            raise EventRejected(NULL_VALUE, "context")
        if not isinstance(context, dict):
            raise EventRejected(INVALID_TYPE, "context")
        return self._check_object(self.batch_context, context, "context")

    def validate_event(
        self,
        event: Any,
        context: Mapping[str, JSONScalar],
        now: datetime,
        max_age: timedelta,
        max_skew: timedelta,
    ) -> ValidEvent:
        """Valida um evento do lote, já com o contexto do lote validado por
        validate_batch_context; lança EventRejected com o primeiro erro."""
        if not isinstance(event, dict):
            raise EventRejected(EVENT_NOT_OBJECT, "event")
        # Campo fora da lista (inclusive session_id ou um "context" por evento,
        # que pertencem ao lote) é recusado, não ignorado.
        for key in sorted(event):
            if key not in EVENT_FIELDS:
                raise EventRejected(UNKNOWN_FIELD, _safe_key(key))
        for key in ("name", "properties"):
            if key not in event:
                raise EventRejected(MISSING_FIELD, key)

        name = event["name"]
        if name is None:
            raise EventRejected(NULL_VALUE, "name")
        if not isinstance(name, str):
            raise EventRejected(INVALID_TYPE, "name")
        spec = self.events.get(name)
        if spec is None:
            raise EventRejected(UNKNOWN_EVENT, "name")

        fields = self._check_object(
            self.event_context,
            {key: event[key] for key in EVENT_CONTEXT_FIELDS if key in event},
            None,
        )

        try:
            moment = parse_timestamp(str(fields["timestamp"]))
        except ValueError:
            raise EventRejected(INVALID_TIMESTAMP, "timestamp")
        if moment > now + max_skew or moment < now - max_age:
            raise EventRejected(TIMESTAMP_OUT_OF_WINDOW, "timestamp")

        properties = event["properties"]
        if properties is None:
            raise EventRejected(NULL_VALUE, "properties")
        if not isinstance(properties, dict):
            raise EventRejected(INVALID_TYPE, "properties")
        clean_properties = self._check_object(spec.properties, properties, "properties")

        layout = fields.get("layout")
        storefront = context.get("storefront")
        return ValidEvent(
            event_id=str(fields["event_id"]),
            name=name,
            session_id=str(context["session_id"]),
            ts=format_timestamp(moment),
            app_version=str(context["app_version"]),
            build=str(context["build"]),
            os_version=str(context["os_version"]),
            device_family=str(context["device_family"]),
            storefront=None if storefront is None else str(storefront),
            layout=None if layout is None else str(layout),
            subscription_state=str(fields["subscription_state"]),
            properties=clean_properties,
        )
