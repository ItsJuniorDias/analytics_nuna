"""Consultas agregadas das rotas /v1/stats.

Todo valor vindo da requisição entra como parâmetro (?). Os únicos trechos de
SQL montados em Python são fragmentos constantes escolhidos por opções já
validadas (agrupar por dia ou não, coluna de contexto de uma lista fixa) e a
quantidade de placeholders de um IN.

Nenhuma rota devolve session_id ou event_id. Todas são contagens, menos
`paywall_session_list`: lançamentos anônimos que viram o paywall, com os
próprios eventos, sem id nenhum e até 50 por vez.
"""

import itertools
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .catalog import Catalog, JSONScalar, PropertySpec

MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 30
MIN_FUNNEL_STEPS = 2
MAX_FUNNEL_STEPS = 10

# Colunas de contexto que podem ser usadas em `by`. event_id, session_id e
# timestamp ficam de fora: agrupar por eles seria listar eventos individuais.
GROUPABLE_CONTEXT_COLUMNS = {
    "app_version": "app_version",
    "build": "build",
    "os_version": "os_version",
    "device_family": "device_family",
    "storefront": "storefront",
    "layout": "layout",
    "subscription_state": "subscription_state",
}


class StatsQueryError(ValueError):
    """Parâmetro inválido; vira HTTP 422 com esta mensagem."""


# Período ---------------------------------------------------------------------


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusivo

    @property
    def start_ts(self) -> str:
        return "%sT00:00:00.000Z" % self.start.isoformat()

    @property
    def end_ts_exclusive(self) -> str:
        return "%sT00:00:00.000Z" % (self.end + timedelta(days=1)).isoformat()

    @property
    def bounds(self) -> Tuple[str, str]:
        return (self.start_ts, self.end_ts_exclusive)

    def days(self) -> List[str]:
        count = (self.end - self.start).days + 1
        return [(self.start + timedelta(days=i)).isoformat() for i in range(count)]


def _parse_day(raw: str, label: str) -> date:
    try:
        if len(raw) != 10:
            raise ValueError
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise StatsQueryError("invalid_date: %s (use YYYY-MM-DD, UTC)" % label)


def resolve_range(raw_from: Optional[str], raw_to: Optional[str], today: date) -> DateRange:
    end = _parse_day(raw_to, "to") if raw_to else today
    start = _parse_day(raw_from, "from") if raw_from else end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start > end:
        raise StatsQueryError("invalid_range: from is after to")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise StatsQueryError("invalid_range: at most %d days" % MAX_RANGE_DAYS)
    return DateRange(start, end)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / float(denominator), 4)


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _typed(spec: Optional[PropertySpec], value: Any) -> Any:
    # json_extract devolve 1/0 para true/false; o painel precisa do boolean.
    if value is None or spec is None:
        return value
    if spec.type == "boolean":
        return bool(value)
    return value


def _count_by_property(
    conn: sqlite3.Connection, catalog: Catalog, rng: DateRange, name: str, prop: str
) -> List[Dict[str, Any]]:
    spec = catalog.property_spec(name, prop)
    rows = conn.execute(
        "SELECT json_extract(properties, ?) AS value, COUNT(*) AS n FROM events"
        " WHERE name = ? AND ts >= ? AND ts < ?"
        " GROUP BY value ORDER BY n DESC, value",
        ("$." + prop, name) + rng.bounds,
    ).fetchall()
    return [{"value": _typed(spec, r["value"]), "count": int(r["n"])} for r in rows]


# Visão geral -----------------------------------------------------------------


def overview(conn: sqlite3.Connection, rng: DateRange) -> Dict[str, Any]:
    per_day = {
        r["day"]: (int(r["events"]), int(r["sessions"]))
        for r in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS events,"
            " COUNT(DISTINCT session_id) AS sessions"
            " FROM events WHERE ts >= ? AND ts < ? GROUP BY day",
            rng.bounds,
        )
    }
    totals = conn.execute(
        "SELECT COUNT(*) AS events, COUNT(DISTINCT session_id) AS sessions"
        " FROM events WHERE ts >= ? AND ts < ?",
        rng.bounds,
    ).fetchone()
    top = conn.execute(
        "SELECT name, COUNT(*) AS n FROM events WHERE ts >= ? AND ts < ?"
        " GROUP BY name ORDER BY n DESC, name",
        rng.bounds,
    ).fetchall()
    # País da conta da App Store. NULL (StoreKit não informou, ou evento de
    # antes do campo existir) vira uma linha própria, no fim.
    storefronts = conn.execute(
        "SELECT storefront, COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS events"
        " FROM events WHERE ts >= ? AND ts < ?"
        " GROUP BY storefront ORDER BY storefront IS NULL, sessions DESC, storefront",
        rng.bounds,
    ).fetchall()
    return {
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        # Sessão que atravessa a meia-noite conta nos dois dias, mas uma vez só
        # no total do período.
        "totals": {"events": int(totals["events"]), "sessions": int(totals["sessions"])},
        "days": [
            {"day": d, "events": per_day.get(d, (0, 0))[0], "sessions": per_day.get(d, (0, 0))[1]}
            for d in rng.days()
        ],
        "top_events": [{"name": r["name"], "count": int(r["n"])} for r in top],
        "storefronts": [
            {"storefront": r["storefront"], "sessions": int(r["sessions"]), "events": int(r["events"])}
            for r in storefronts
        ],
    }


# Contagem de um evento -------------------------------------------------------


def event_counts(
    conn: sqlite3.Connection,
    catalog: Catalog,
    rng: DateRange,
    name: Optional[str],
    group: str,
    by: Optional[str],
) -> Dict[str, Any]:
    if not name:
        raise StatsQueryError("missing_parameter: name")
    if not catalog.has_event(name):
        raise StatsQueryError("unknown_event: name")
    if group not in ("day", "total"):
        raise StatsQueryError("invalid_group: use day or total")

    params: List[Any] = []
    select: List[str] = []
    grouping: List[str] = []
    spec: Optional[PropertySpec] = None
    if group == "day":
        select.append("substr(ts, 1, 10) AS day")
        grouping.append("day")
    if by:
        spec = catalog.property_spec(name, by)
        if spec is not None:
            select.append("json_extract(properties, ?) AS value")
            params.append("$." + by)
        elif by in GROUPABLE_CONTEXT_COLUMNS:
            select.append("%s AS value" % GROUPABLE_CONTEXT_COLUMNS[by])
        else:
            raise StatsQueryError("unknown_property: by")
        grouping.append("value")
    select.append("COUNT(*) AS n")

    sql = "SELECT %s FROM events WHERE name = ? AND ts >= ? AND ts < ?" % ", ".join(select)
    params.extend((name,) + rng.bounds)
    if grouping:
        sql += " GROUP BY %s ORDER BY %s" % (", ".join(grouping), ", ".join(grouping))
    rows = conn.execute(sql, params).fetchall()

    out_rows: List[Dict[str, Any]] = []
    if not grouping:
        out_rows = [{"count": int(rows[0]["n"])}]
    elif group == "day" and not by:
        counts = {r["day"]: int(r["n"]) for r in rows}
        out_rows = [{"day": d, "count": counts.get(d, 0)} for d in rng.days()]
    else:
        for r in rows:
            if int(r["n"]) == 0:
                continue
            item: Dict[str, Any] = {"count": int(r["n"])}
            if group == "day":
                item["day"] = r["day"]
            if by:
                item["value"] = _typed(spec, r["value"])
            out_rows.append(item)
        if by and group == "total":
            out_rows.sort(key=lambda item: (-item["count"], str(item.get("value"))))

    return {
        "name": name,
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        "group": group,
        "by": by or None,
        "total": sum(item["count"] for item in out_rows),
        "rows": out_rows,
    }


# Funil -----------------------------------------------------------------------


@dataclass
class FunnelStepSpec:
    raw: str
    name: str
    filters: Dict[str, JSONScalar] = field(default_factory=dict)

    def matches(self, name: str, properties: Optional[Dict[str, Any]]) -> bool:
        if name != self.name:
            return False
        if not self.filters:
            return True
        props = properties or {}
        for key, expected in self.filters.items():
            actual = props.get(key)
            # type() e não só ==: em Python True == 1, e has_query=true não pode
            # casar com um inteiro 1.
            if type(actual) is not type(expected) or actual != expected:
                return False
        return True


def parse_funnel_steps(catalog: Catalog, raw: Optional[str]) -> List[FunnelStepSpec]:
    """steps=nome[:prop=valor[:prop=valor]],nome,...

    Ex.: onboarding_started:trigger=first_run,onboarding_completed,paywall_viewed:source=post_onboarding
    """
    if not raw:
        raise StatsQueryError("missing_parameter: steps")
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < MIN_FUNNEL_STEPS or len(parts) > MAX_FUNNEL_STEPS:
        raise StatsQueryError(
            "invalid_steps: use %d to %d comma-separated steps" % (MIN_FUNNEL_STEPS, MAX_FUNNEL_STEPS)
        )
    steps: List[FunnelStepSpec] = []
    for position, part in enumerate(parts, start=1):
        segments = part.split(":")
        name = segments[0].strip()
        if not catalog.has_event(name):
            raise StatsQueryError("unknown_event: step %d" % position)
        step = FunnelStepSpec(raw=part, name=name)
        for segment in segments[1:]:
            prop, eq, value = segment.partition("=")
            prop = prop.strip()
            spec = catalog.property_spec(name, prop)
            if not eq or spec is None:
                raise StatsQueryError("unknown_property: step %d" % position)
            if prop in step.filters:
                raise StatsQueryError("duplicate_filter: step %d" % position)
            try:
                step.filters[prop] = spec.parse_text(value.strip())
            except ValueError as exc:
                raise StatsQueryError("invalid_filter_value: step %d (%s)" % (position, exc))
        steps.append(step)
    return steps


def funnel(
    conn: sqlite3.Connection, catalog: Catalog, rng: DateRange, raw_steps: Optional[str]
) -> Dict[str, Any]:
    """Sessões que fizeram os passos nesta ordem (não precisam ser seguidos).

    O funil vive dentro de uma sessão: sem identificador persistente não há
    como seguir a mesma família entre lançamentos, e isso é intencional.
    """
    steps = parse_funnel_steps(catalog, raw_steps)
    names = sorted({s.name for s in steps})
    rank: Dict[str, int] = {}
    for index, step in enumerate(steps):
        rank.setdefault(step.name, index)
    needs_properties = any(s.filters for s in steps)

    sql = (
        "SELECT session_id, name, ts, %s AS properties FROM events"
        " WHERE name IN (%s) AND ts >= ? AND ts < ?"
        " ORDER BY session_id, ts"
    ) % ("properties" if needs_properties else "NULL", _placeholders(len(names)))
    cursor = conn.execute(sql, tuple(names) + rng.bounds)

    reached = [0] * len(steps)
    for _, rows in itertools.groupby(cursor, key=lambda r: r["session_id"]):
        # Empate de timestamp (mesmo milissegundo) segue a ordem do funil:
        # eventos simultâneos não devem quebrar a sequência por acaso.
        ordered = sorted(rows, key=lambda r: (r["ts"], rank[r["name"]]))
        k = 0
        for row in ordered:
            props = json.loads(row["properties"]) if needs_properties else None
            if steps[k].matches(row["name"], props):
                k += 1
                if k == len(steps):
                    break
        for i in range(k):
            reached[i] += 1

    first = reached[0] if reached else 0
    out_steps = []
    for index, step in enumerate(steps):
        out_steps.append(
            {
                "step": step.raw,
                "name": step.name,
                "filters": step.filters,
                "sessions": reached[index],
                "conversion_from_previous": None if index == 0 else _rate(reached[index], reached[index - 1]),
                "conversion_from_first": _rate(reached[index], first),
            }
        )
    return {"from": rng.start.isoformat(), "to": rng.end.isoformat(), "steps": out_steps}


# Livros ----------------------------------------------------------------------


def books(conn: sqlite3.Connection, rng: DateRange) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT json_extract(properties, ?) AS book_id,"
        " SUM(CASE WHEN name = ? THEN 1 ELSE 0 END) AS opens,"
        " COUNT(DISTINCT CASE WHEN name = ? THEN session_id END) AS sessions,"
        " SUM(CASE WHEN name = ? THEN 1 ELSE 0 END) AS completions"
        " FROM events WHERE name IN (?, ?) AND ts >= ? AND ts < ?"
        " GROUP BY book_id ORDER BY opens DESC, completions DESC, book_id",
        ("$.book_id", "book_opened", "book_opened", "book_completed", "book_opened", "book_completed")
        + rng.bounds,
    ).fetchall()
    return {
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        "books": [
            {
                "book_id": r["book_id"],
                "opens": int(r["opens"]),
                "sessions": int(r["sessions"]),
                "completions": int(r["completions"]),
                "completion_rate": _rate(int(r["completions"]), int(r["opens"])),
            }
            for r in rows
            if r["book_id"] is not None
        ],
    }


# Paywall ---------------------------------------------------------------------

GATE_EVENTS = (
    ("parental_gate_shown", "shown"),
    ("parental_gate_passed", "passed"),
    ("parental_gate_failed", "failed"),
    ("parental_gate_cancelled", "cancelled"),
)
PURCHASE_EVENTS = ("purchase_completed", "purchase_pending", "purchase_cancelled", "purchase_failed")


def _grouped_pairs(
    conn: sqlite3.Connection, rng: DateRange, names: Sequence[str], prop: str
) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT json_extract(properties, ?) AS value, name, COUNT(*) AS n FROM events"
        " WHERE name IN (%s) AND ts >= ? AND ts < ?"
        " GROUP BY value, name" % _placeholders(len(names)),
        ("$." + prop,) + tuple(names) + rng.bounds,
    ).fetchall()


def paywall(conn: sqlite3.Connection, catalog: Catalog, rng: DateRange) -> Dict[str, Any]:
    views_by_source = _count_by_property(conn, catalog, rng, "paywall_viewed", "source")
    views_total = sum(item["count"] for item in views_by_source)
    taps_by_plan = _count_by_property(conn, catalog, rng, "subscribe_tapped", "plan")

    purpose_spec = catalog.property_spec("parental_gate_shown", "purpose")
    purposes = list(purpose_spec.enum or ()) if purpose_spec is not None else []
    gate = {p: {"purpose": p, "shown": 0, "passed": 0, "failed": 0, "cancelled": 0} for p in purposes}
    column_for = dict(GATE_EVENTS)
    for r in _grouped_pairs(conn, rng, [n for n, _ in GATE_EVENTS], "purpose"):
        if r["value"] not in gate:
            continue
        gate[r["value"]][column_for[r["name"]]] = int(r["n"])
    gate_rows = []
    for p in purposes:
        row = dict(gate[p])
        row["pass_rate"] = _rate(row["passed"], row["shown"])
        gate_rows.append(row)

    purchases = []
    purchases_completed = 0
    for r in _grouped_pairs(conn, rng, PURCHASE_EVENTS, "plan"):
        result = r["name"][len("purchase_"):]
        count = int(r["n"])
        purchases.append({"plan": r["value"], "result": result, "count": count})
        if result == "completed":
            purchases_completed += count
    purchases.sort(key=lambda item: (item["plan"], PURCHASE_EVENTS.index("purchase_" + item["result"])))

    return {
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        "views_total": views_total,
        "views_by_source": views_by_source,
        "subscribe_taps_total": sum(item["count"] for item in taps_by_plan),
        "subscribe_taps_by_plan": taps_by_plan,
        "gate": gate_rows,
        "purchases": purchases,
        "purchases_completed": purchases_completed,
        # Aproximação: a compra pode vir de uma visualização fora do período.
        "view_to_purchase_rate": _rate(purchases_completed, views_total),
        "closes_by_reason": _count_by_property(conn, catalog, rng, "paywall_closed", "reason"),
        "restores_by_result": _count_by_property(conn, catalog, rng, "restore_finished", "result"),
        "by_storefront": _paywall_by_storefront(conn, rng),
    }


def _paywall_by_storefront(conn: sqlite3.Connection, rng: DateRange) -> List[Dict[str, Any]]:
    """Visualizações, toques em assinar e compras concluídas por país da loja,
    do país que mais vende para o que menos vende."""
    rows = conn.execute(
        "SELECT storefront,"
        " SUM(name = 'paywall_viewed') AS views,"
        " SUM(name = 'subscribe_tapped') AS taps,"
        " SUM(name = 'purchase_completed') AS purchases"
        " FROM events"
        " WHERE name IN ('paywall_viewed', 'subscribe_tapped', 'purchase_completed')"
        " AND ts >= ? AND ts < ?"
        " GROUP BY storefront"
        " ORDER BY storefront IS NULL, purchases DESC, views DESC, storefront",
        rng.bounds,
    ).fetchall()
    return [
        {
            "storefront": r["storefront"],
            "views": int(r["views"]),
            "subscribe_taps": int(r["taps"]),
            "purchases_completed": int(r["purchases"]),
            "view_to_purchase_rate": _rate(int(r["purchases"]), int(r["views"])),
        }
        for r in rows
    ]


# Conversão do paywall --------------------------------------------------------

# O caminho de uma compra, na ordem. `plan_selected` fica de fora: é opcional
# (o plano pré-selecionado basta) e cortaria quem assina sem trocar de cartão.
# O portão dos pais entra filtrado: o mesmo evento também aparece para
# gerenciar assinatura e para abrir link externo.
PAYWALL_STEPS: Tuple[Tuple[str, str], ...] = (
    ("viewed", "paywall_viewed"),
    ("subscribe_tapped", "subscribe_tapped"),
    ("gate_passed", "parental_gate_passed:purpose=subscribe"),
    ("purchased", "purchase_completed"),
)

# Por onde quebrar a conversão. Tudo vem da PRIMEIRA `paywall_viewed` da
# sessão: as propriedades dela (origem, trial, faixas da jornada) ou o
# contexto gravado junto (país da loja, aparelho, versão).
CONVERSION_SEGMENTS: Dict[str, Tuple[str, str]] = {
    "storefront": ("context", "storefront"),
    "source": ("property", "source"),
    "trial_eligible": ("property", "trial_eligible"),
    "install_age_bucket": ("property", "install_age_bucket"),
    "prior_paywall_views_bucket": ("property", "prior_paywall_views_bucket"),
    "books_completed_bucket": ("property", "books_completed_bucket"),
    "device_family": ("context", "device_family"),
    "app_version": ("context", "app_version"),
}

SESSION_OUTCOMES = ("all", "purchased", "not_purchased")
DEFAULT_SESSION_LIMIT = 50
MAX_SESSION_LIMIT = 50
MAX_EVENTS_PER_SESSION = 300


@dataclass
class _PaywallSession:
    session_id: str
    reached: int  # passos de PAYWALL_STEPS cumpridos em ordem, 1 a 4
    view: sqlite3.Row  # a primeira paywall_viewed da sessão no período
    view_properties: Dict[str, Any]


def _paywall_sessions(conn: sqlite3.Connection, catalog: Catalog, rng: DateRange) -> List[_PaywallSession]:
    """Cada sessão que viu o paywall no período, com até onde chegou.

    Mesma regra do funil genérico: passos em ordem, dentro da sessão. Compra
    feita em outro lançamento do app não é ligada a esta visualização, e esse
    é o preço de não ter identificador persistente.
    """
    steps = parse_funnel_steps(catalog, ",".join(raw for _, raw in PAYWALL_STEPS))
    names = sorted({s.name for s in steps})
    rank: Dict[str, int] = {}
    for index, step in enumerate(steps):
        rank.setdefault(step.name, index)

    cursor = conn.execute(
        "SELECT session_id, name, ts, properties, storefront, device_family, app_version FROM events"
        " WHERE name IN (%s) AND ts >= ? AND ts < ?"
        " ORDER BY session_id, ts" % _placeholders(len(names)),
        tuple(names) + rng.bounds,
    )
    sessions: List[_PaywallSession] = []
    for session_id, rows in itertools.groupby(cursor, key=lambda r: r["session_id"]):
        ordered = sorted(rows, key=lambda r: (r["ts"], rank[r["name"]]))
        k = 0
        view: Optional[sqlite3.Row] = None
        view_properties: Dict[str, Any] = {}
        for row in ordered:
            props = json.loads(row["properties"])
            if steps[k].matches(row["name"], props):
                if k == 0:
                    view, view_properties = row, props
                k += 1
                if k == len(steps):
                    break
        if view is not None:
            sessions.append(_PaywallSession(session_id, k, view, view_properties))
    return sessions


def _segment_value(session: _PaywallSession, by: str) -> Any:
    kind, key = CONVERSION_SEGMENTS[by]
    if kind == "context":
        return session.view[key]
    return session.view_properties.get(key)


def _reached_counts(reached: Sequence[int]) -> List[int]:
    return [sum(1 for r in reached if r > i) for i in range(len(PAYWALL_STEPS))]


def paywall_conversion(
    conn: sqlite3.Connection, catalog: Catalog, rng: DateRange, by: Optional[str]
) -> Dict[str, Any]:
    """Funil de compra por sessão (visualização → assinar → portão → compra),
    no total e, com `by`, quebrado por um segmento da primeira visualização."""
    if by is not None and by not in CONVERSION_SEGMENTS:
        raise StatsQueryError("invalid_by: use one of %s" % ", ".join(sorted(CONVERSION_SEGMENTS)))

    sessions = _paywall_sessions(conn, catalog, rng)
    counts = _reached_counts([s.reached for s in sessions])
    steps = [
        {
            "key": key,
            "name": raw.split(":")[0],
            "sessions": counts[index],
            "conversion_from_previous": None if index == 0 else _rate(counts[index], counts[index - 1]),
            "conversion_from_first": _rate(counts[index], counts[0]),
        }
        for index, (key, raw) in enumerate(PAYWALL_STEPS)
    ]

    segments: List[Dict[str, Any]] = []
    if by is not None:
        groups: Dict[Any, List[int]] = {}
        for session in sessions:
            groups.setdefault(_segment_value(session, by), []).append(session.reached)
        for value, reached in groups.items():
            c = _reached_counts(reached)
            segments.append(
                {
                    "value": value,
                    "sessions": c[0],
                    "subscribe_tapped": c[1],
                    "gate_passed": c[2],
                    "purchased": c[3],
                    "conversion": _rate(c[3], c[0]),
                }
            )
        # Faixas na ordem do catálogo (lt_1d, 1_3d…), o resto do maior para o
        # menor. Sem valor (app antigo, loja não informada) sempre no fim.
        kind, key = CONVERSION_SEGMENTS[by]
        spec = catalog.property_spec("paywall_viewed", key) if kind == "property" else None
        order = list(spec.enum) if spec is not None and spec.enum else None

        def sort_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
            value = row["value"]
            if order is not None:
                return (value not in order, order.index(value) if value in order else 0)
            return (value is None, -row["sessions"], str(value))

        segments.sort(key=sort_key)

    return {
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        "by": by,
        "steps": steps,
        "segments": segments,
    }


def paywall_session_list(
    conn: sqlite3.Connection,
    catalog: Catalog,
    rng: DateRange,
    outcome: Optional[str],
    limit: Optional[int],
) -> Dict[str, Any]:
    """Sessões que viram o paywall, da mais recente para a mais antiga, cada
    uma com os próprios eventos em ordem.

    É a única rota que devolve linhas individuais, e sem nenhum id: nem
    session_id nem event_id saem daqui. Cada linha é um lançamento anônimo do
    app, não uma pessoa, e até 50 por vez.
    """
    outcome = outcome or "all"
    if outcome not in SESSION_OUTCOMES:
        raise StatsQueryError("invalid_outcome: use %s" % ", ".join(SESSION_OUTCOMES))
    limit = DEFAULT_SESSION_LIMIT if limit is None else limit
    if limit < 1 or limit > MAX_SESSION_LIMIT:
        raise StatsQueryError("invalid_limit: use 1 to %d" % MAX_SESSION_LIMIT)

    last = len(PAYWALL_STEPS)
    sessions = _paywall_sessions(conn, catalog, rng)
    if outcome == "purchased":
        sessions = [s for s in sessions if s.reached == last]
    elif outcome == "not_purchased":
        sessions = [s for s in sessions if s.reached < last]
    sessions.sort(key=lambda s: s.view["ts"], reverse=True)
    shown = sessions[:limit]

    timelines: Dict[str, List[Dict[str, Any]]] = {s.session_id: [] for s in shown}
    if shown:
        cursor = conn.execute(
            "SELECT session_id, name, ts, subscription_state, properties FROM events"
            " WHERE session_id IN (%s) ORDER BY session_id, ts" % _placeholders(len(shown)),
            tuple(timelines),
        )
        for r in cursor:
            timelines[r["session_id"]].append(
                {
                    "name": r["name"],
                    "ts": r["ts"],
                    "subscription_state": r["subscription_state"],
                    "properties": json.loads(r["properties"]),
                }
            )

    rows = []
    for s in shown:
        props = s.view_properties
        events = timelines[s.session_id]
        rows.append(
            {
                "first_view_at": s.view["ts"],
                "storefront": s.view["storefront"],
                "device_family": s.view["device_family"],
                "app_version": s.view["app_version"],
                "source": props.get("source"),
                "trial_eligible": props.get("trial_eligible"),
                "install_age_bucket": props.get("install_age_bucket"),
                "prior_paywall_views_bucket": props.get("prior_paywall_views_bucket"),
                "books_completed_bucket": props.get("books_completed_bucket"),
                "furthest_step": PAYWALL_STEPS[s.reached - 1][0],
                "purchased": s.reached == last,
                "event_count": len(events),
                "events": events[:MAX_EVENTS_PER_SESSION],
            }
        )
    return {
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        "outcome": outcome,
        "total": len(sessions),
        "sessions": rows,
    }
