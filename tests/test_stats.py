"""Rotas /v1/stats sobre dados semeados com datas fixas."""

import pytest

from .helpers import admin_headers, make_event, new_uuid, post_batch, sample_properties

RANGE = {"from": "2026-03-01", "to": "2026-03-04"}

S1, S2, S3, S4, S5 = (new_uuid() for _ in range(5))
BOOK_A = "o-quintal-da-nuna"
BOOK_B = "a-lua-da-nuna"


def ev(catalog, name, session, ts, device="phone", **props):
    properties = sample_properties(catalog, name, include_optional=False)
    properties.update(props)
    return make_event(catalog, name, properties, session_id=session, timestamp=ts, device_family=device)


@pytest.fixture
def seeded(make_client, catalog):
    # Janela larga: os eventos têm datas fixas no passado.
    client = make_client(max_event_age_days=36500)
    e = lambda *a, **k: ev(catalog, *a, **k)  # noqa: E731
    events = [
        # S1, 1º de março: jornada completa até a compra e leitura até o fim.
        e("app_opened", S1, "2026-03-01T10:00:00.000Z"),
        e("onboarding_started", S1, "2026-03-01T10:00:01.000Z", trigger="first_run"),
        e("paywall_viewed", S1, "2026-03-01T10:01:00.000Z", source="post_onboarding"),
        e("subscribe_tapped", S1, "2026-03-01T10:02:00.000Z", plan="annual"),
        e("parental_gate_shown", S1, "2026-03-01T10:02:01.000Z", purpose="subscribe"),
        e("parental_gate_passed", S1, "2026-03-01T10:02:30.000Z", purpose="subscribe"),
        e("purchase_completed", S1, "2026-03-01T10:03:00.000Z", plan="annual"),
        e("book_opened", S1, "2026-03-01T10:05:00.000Z", book_id=BOOK_A, two_halves=False),
        e("book_completed", S1, "2026-03-01T10:10:00.000Z", book_id=BOOK_A),
        # S2 atravessa a meia-noite, num iPad.
        e("app_opened", S2, "2026-03-01T23:59:00.000Z", device="pad"),
        e("paywall_viewed", S2, "2026-03-02T00:00:30.000Z", device="pad", source="home_header"),
        e("parental_gate_shown", S2, "2026-03-02T00:00:40.000Z", device="pad", purpose="subscribe"),
        e("parental_gate_failed", S2, "2026-03-02T00:00:50.000Z", device="pad", purpose="subscribe"),
        e("parental_gate_cancelled", S2, "2026-03-02T00:01:00.000Z", device="pad", purpose="subscribe"),
        e("book_opened", S2, "2026-03-02T00:02:00.000Z", device="pad", book_id=BOOK_A, two_halves=False),
        e("book_opened", S2, "2026-03-02T00:05:00.000Z", device="pad", book_id=BOOK_B, two_halves=False),
        # S3: toque em assinar ANTES do paywall (fora de ordem para o funil).
        e("subscribe_tapped", S3, "2026-03-02T09:00:00.000Z", plan="monthly"),
        e("paywall_viewed", S3, "2026-03-02T09:01:00.000Z", source="locked_book", book_id=BOOK_B),
        e("purchase_failed", S3, "2026-03-02T09:02:00.000Z", plan="monthly"),
        # S4: paywall e toque no mesmo milissegundo.
        e("subscribe_tapped", S4, "2026-03-02T10:00:00.000Z", plan="monthly"),
        e("paywall_viewed", S4, "2026-03-02T10:00:00.000Z", source="locked_book"),
        e("purchase_cancelled", S4, "2026-03-02T10:01:00.000Z", plan="monthly"),
        # S5: fora do período, nas duas bordas.
        e("app_opened", S5, "2026-02-28T23:59:59.999Z"),
        e("book_opened", S5, "2026-03-05T00:00:00.000Z", book_id=BOOK_B),
    ]
    response = post_batch(client, events)
    assert response.json() == {"accepted": len(events), "duplicates": 0, "rejected": []}
    return client


def get(client, path, **params):
    query = dict(RANGE)
    query.update(params)
    response = client.get(path, params=query, headers=admin_headers())
    assert response.status_code == 200, response.text
    return response.json()


# Visão geral ---------------------------------------------------------------


def test_overview_counts_events_and_sessions_per_day(seeded):
    data = get(seeded, "/v1/stats/overview")
    assert data["from"] == "2026-03-01" and data["to"] == "2026-03-04"
    assert data["days"] == [
        {"day": "2026-03-01", "events": 10, "sessions": 2},
        {"day": "2026-03-02", "events": 12, "sessions": 3},
        {"day": "2026-03-03", "events": 0, "sessions": 0},
        {"day": "2026-03-04", "events": 0, "sessions": 0},
    ]
    # S2 aparece nos dois dias, mas é uma sessão só no período.
    assert data["totals"] == {"events": 22, "sessions": 4}
    assert data["top_events"][:3] == [
        {"name": "paywall_viewed", "count": 4},
        {"name": "book_opened", "count": 3},
        {"name": "subscribe_tapped", "count": 3},
    ]
    assert sum(item["count"] for item in data["top_events"]) == 22


def test_default_range_is_last_30_days(seeded):
    response = seeded.get("/v1/stats/overview", headers=admin_headers())
    data = response.json()
    assert len(data["days"]) == 30
    assert data["totals"] == {"events": 0, "sessions": 0}


# Contagem de evento ----------------------------------------------------------


def test_event_counts_per_day(seeded):
    data = get(seeded, "/v1/stats/events", name="book_opened")
    assert data["total"] == 3
    assert data["rows"] == [
        {"day": "2026-03-01", "value": None, "count": 1},
        {"day": "2026-03-02", "value": None, "count": 2},
        {"day": "2026-03-03", "value": None, "count": 0},
        {"day": "2026-03-04", "value": None, "count": 0},
    ]


def test_event_counts_total(seeded):
    data = get(seeded, "/v1/stats/events", name="paywall_viewed", group="total")
    assert data["total"] == 4
    assert data["rows"] == [{"day": None, "value": None, "count": 4}]


def test_event_counts_by_property(seeded):
    data = get(seeded, "/v1/stats/events", name="book_opened", group="total", by="book_id")
    assert data["by"] == "book_id"
    assert data["rows"] == [
        {"day": None, "value": BOOK_A, "count": 2},
        {"day": None, "value": BOOK_B, "count": 1},
    ]


def test_event_counts_by_boolean_returns_booleans(seeded):
    data = get(seeded, "/v1/stats/events", name="book_opened", group="total", by="two_halves")
    assert data["rows"] == [{"day": None, "value": False, "count": 3}]


def test_event_counts_by_absent_optional_property(seeded):
    data = get(seeded, "/v1/stats/events", name="book_opened", group="total", by="collection_id")
    assert data["rows"] == [{"day": None, "value": None, "count": 3}]


def test_event_counts_by_context_field(seeded):
    data = get(seeded, "/v1/stats/events", name="book_opened", group="total", by="device_family")
    assert data["rows"] == [
        {"day": None, "value": "pad", "count": 2},
        {"day": None, "value": "phone", "count": 1},
    ]


def test_event_counts_per_day_and_property(seeded):
    data = get(seeded, "/v1/stats/events", name="book_opened", by="book_id")
    # Ordenado por dia e depois pelo valor ("a-lua..." antes de "o-quintal...").
    assert data["rows"] == [
        {"day": "2026-03-01", "value": BOOK_A, "count": 1},
        {"day": "2026-03-02", "value": BOOK_B, "count": 1},
        {"day": "2026-03-02", "value": BOOK_A, "count": 1},
    ]
    assert data["total"] == 3


# Funil -----------------------------------------------------------------------


def _sessions(data):
    return [step["sessions"] for step in data["steps"]]


def test_funnel_respects_order_within_session(seeded):
    data = get(seeded, "/v1/stats/funnel", steps="paywall_viewed,subscribe_tapped,purchase_completed")
    # S1 faz tudo; S2 só vê o paywall; S3 tocou antes de ver (não conta);
    # S4 empata no milissegundo e conta; nenhuma além de S1 compra.
    assert _sessions(data) == [4, 2, 1]
    assert [s["conversion_from_previous"] for s in data["steps"]] == [None, 0.5, 0.5]
    assert [s["conversion_from_first"] for s in data["steps"]] == [1.0, 0.5, 0.25]
    assert [s["name"] for s in data["steps"]] == ["paywall_viewed", "subscribe_tapped", "purchase_completed"]


def test_funnel_with_property_filter(seeded):
    data = get(seeded, "/v1/stats/funnel", steps="paywall_viewed:source=locked_book,subscribe_tapped")
    assert _sessions(data) == [2, 1]
    assert data["steps"][0]["filters"] == {"source": "locked_book"}


def test_funnel_with_multiple_filters_and_booleans(seeded):
    data = get(
        seeded,
        "/v1/stats/funnel",
        steps="parental_gate_shown:purpose=subscribe,parental_gate_passed:purpose=subscribe:was_paused=true",
    )
    # sample_properties usa True para booleans, então was_paused=true casa.
    assert _sessions(data) == [2, 1]
    data = get(
        seeded,
        "/v1/stats/funnel",
        steps="parental_gate_shown:purpose=subscribe,parental_gate_passed:was_paused=false",
    )
    assert _sessions(data) == [2, 0]


def test_funnel_repeated_step_needs_two_events(seeded):
    assert _sessions(get(seeded, "/v1/stats/funnel", steps="book_opened,book_opened")) == [2, 1]


def test_funnel_does_not_cross_sessions(seeded):
    # S3 falhou a compra, mas o app_opened daquele aparelho foi em outra sessão
    # (aqui nem existe): sem identificador persistente, não há como juntar.
    assert _sessions(get(seeded, "/v1/stats/funnel", steps="app_opened,purchase_failed")) == [2, 0]


def test_funnel_excludes_events_outside_range(seeded):
    data = get(seeded, "/v1/stats/funnel", steps="app_opened,book_opened")
    assert _sessions(data) == [2, 2]
    data = get(seeded, "/v1/stats/funnel", steps="app_opened,book_opened", **{"from": "2026-02-28", "to": "2026-03-05"})
    assert _sessions(data) == [3, 3]


# Livros e paywall --------------------------------------------------------------


def test_books(seeded):
    data = get(seeded, "/v1/stats/books")
    assert data["books"] == [
        {"book_id": BOOK_A, "opens": 2, "sessions": 2, "completions": 1, "completion_rate": 0.5},
        {"book_id": BOOK_B, "opens": 1, "sessions": 1, "completions": 0, "completion_rate": 0.0},
    ]


def test_paywall(seeded):
    data = get(seeded, "/v1/stats/paywall")
    assert data["views_total"] == 4
    assert data["views_by_source"] == [
        {"value": "locked_book", "count": 2},
        {"value": "home_header", "count": 1},
        {"value": "post_onboarding", "count": 1},
    ]
    assert data["subscribe_taps_total"] == 3
    assert data["subscribe_taps_by_plan"] == [{"value": "monthly", "count": 2}, {"value": "annual", "count": 1}]
    assert data["gate"] == [
        {"purpose": "subscribe", "shown": 2, "passed": 1, "failed": 1, "cancelled": 1, "pass_rate": 0.5},
        {"purpose": "manage_subscription", "shown": 0, "passed": 0, "failed": 0, "cancelled": 0, "pass_rate": None},
    ]
    assert data["purchases"] == [
        {"plan": "annual", "result": "completed", "count": 1},
        {"plan": "monthly", "result": "cancelled", "count": 1},
        {"plan": "monthly", "result": "failed", "count": 1},
    ]
    assert data["purchases_completed"] == 1
    assert data["view_to_purchase_rate"] == 0.25
    assert data["closes_by_reason"] == []
    assert data["restores_by_result"] == []


def test_empty_database_returns_zeros(client):
    headers = admin_headers()
    assert client.get("/v1/stats/books", headers=headers).json()["books"] == []
    paywall = client.get("/v1/stats/paywall", headers=headers).json()
    assert paywall["views_total"] == 0 and paywall["view_to_purchase_rate"] is None
    funnel = client.get("/v1/stats/funnel?steps=app_opened,book_opened", headers=headers).json()
    assert [s["sessions"] for s in funnel["steps"]] == [0, 0]
    assert funnel["steps"][1]["conversion_from_previous"] is None


# Parâmetros inválidos ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/stats/overview?from=2026-3-1",
        "/v1/stats/overview?from=01/03/2026",
        "/v1/stats/overview?to=2026-02-30",
        "/v1/stats/overview?from=2026-03-05&to=2026-03-01",
        "/v1/stats/overview?from=2024-01-01&to=2026-01-01",
        "/v1/stats/events",
        "/v1/stats/events?name=app_crashed",
        "/v1/stats/events?name=book_opened&group=week",
        "/v1/stats/events?name=book_opened&by=title",
        "/v1/stats/events?name=book_opened&by=properties",
        "/v1/stats/funnel",
        "/v1/stats/funnel?steps=app_opened",
        "/v1/stats/funnel?steps=" + ",".join(["app_opened"] * 11),
        "/v1/stats/funnel?steps=app_opened,app_crashed",
        "/v1/stats/funnel?steps=paywall_viewed:plan=annual,subscribe_tapped",
        "/v1/stats/funnel?steps=paywall_viewed:source=nowhere,subscribe_tapped",
        "/v1/stats/funnel?steps=paywall_viewed:source,subscribe_tapped",
        "/v1/stats/funnel?steps=paywall_viewed:source=parents:source=parents,subscribe_tapped",
        "/v1/stats/funnel?steps=parental_gate_passed:failed_attempts=abc,purchase_completed",
        "/v1/stats/funnel?steps=parental_gate_passed:failed_attempts=100,purchase_completed",
        "/v1/stats/funnel?steps=parental_gate_passed:was_paused=1,purchase_completed",
        "/v1/stats/funnel?steps=app_opened' OR 1=1 --,book_opened",
    ],
)
def test_invalid_parameters_are_422(client, path):
    response = client.get(path, headers=admin_headers())
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)


def test_integer_filter_in_funnel(seeded):
    data = get(seeded, "/v1/stats/funnel", steps="parental_gate_shown,parental_gate_passed:failed_attempts=0")
    assert _sessions(data) == [2, 1]


def test_stats_responses_are_not_cacheable(seeded):
    response = seeded.get("/v1/stats/overview", params=RANGE, headers=admin_headers())
    assert response.headers["cache-control"] == "no-store"
