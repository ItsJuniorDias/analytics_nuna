"""Cada motivo de rejeição do validador, pela API e direto no catálogo."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.catalog import Catalog, CatalogError, EventRejected
from app.config import DEFAULT_CATALOG_PATH

from .helpers import APAGAR, LOTE, make_batch_context, make_event, now_ts, post_batch, wire


def _set(path, value):
    def mutate(event):
        target = event
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return mutate


def _delete(path):
    def mutate(event):
        target = event
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        del target[keys[-1]]

    return mutate


def _set_batch(key, value):
    """Muda um campo do "context" do lote em que o evento vai."""

    def mutate(event):
        event.setdefault(LOTE, {})[key] = value

    return mutate


def _delete_batch(key):
    def mutate(event):
        event.setdefault(LOTE, {})[key] = APAGAR

    return mutate


# (id, evento base, mutação, motivo esperado)
CASES = [
    # Estrutura do evento
    ("unknown_top_level_field", "app_opened", _set("user_id", "abc"), "unknown_field: user_id"),
    ("unknown_field_with_free_text_name", "app_opened", _set("Maria da Silva", 1), "unknown_field: <invalid_key>"),
    ("missing_name", "app_opened", _delete("name"), "missing_field: name"),
    # O contexto é do lote; um "context" dentro do evento é campo desconhecido.
    ("context_inside_event", "app_opened", _set("context", {}), "unknown_field: context"),
    ("session_id_inside_event", "app_opened", _set("session_id", "5d2c8f7a-1b3e-4c6d-8e9f-0a1b2c3d4e5f"), "unknown_field: session_id"),
    ("missing_properties", "app_opened", _delete("properties"), "missing_field: properties"),
    ("null_name", "app_opened", _set("name", None), "null_value: name"),
    ("name_not_string", "app_opened", _set("name", 7), "invalid_type: name"),
    ("properties_not_object", "app_opened", _set("properties", "x"), "invalid_type: properties"),
    ("null_properties", "app_opened", _set("properties", None), "null_value: properties"),
    # Nome
    ("unknown_event", "app_opened", _set("name", "user_signed_up"), "unknown_event: name"),
    ("event_name_wrong_case", "app_opened", _set("name", "App_Opened"), "unknown_event: name"),
    # Propriedades
    ("unknown_property", "app_opened", _set("properties.search_text", "dinossauro"), "unknown_property: properties.search_text"),
    ("missing_required_property", "app_opened", _delete("properties.onboarding_completed"), "missing_required: properties.onboarding_completed"),
    ("null_optional_property", "screen_viewed", _set("properties.previous_screen", None), "null_value: properties.previous_screen"),
    ("null_required_property", "app_opened", _set("properties.onboarding_completed", None), "null_value: properties.onboarding_completed"),
    ("empty_string_optional_enum", "screen_viewed", _set("properties.previous_screen", ""), "invalid_enum: properties.previous_screen"),
    ("string_for_boolean", "app_opened", _set("properties.onboarding_completed", "true"), "invalid_type: properties.onboarding_completed"),
    ("int_for_boolean", "app_opened", _set("properties.onboarding_completed", 1), "invalid_type: properties.onboarding_completed"),
    ("bool_for_integer", "catalog_loaded", _set("properties.books_total", True), "invalid_type: properties.books_total"),
    ("float_for_integer", "catalog_loaded", _set("properties.books_total", 3.0), "invalid_type: properties.books_total"),
    ("string_for_integer", "catalog_loaded", _set("properties.books_total", "3"), "invalid_type: properties.books_total"),
    ("int_for_string", "screen_viewed", _set("properties.screen", 1), "invalid_type: properties.screen"),
    ("list_for_string", "screen_viewed", _set("properties.screen", ["home"]), "invalid_type: properties.screen"),
    ("invalid_enum", "screen_viewed", _set("properties.screen", "settings"), "invalid_enum: properties.screen"),
    ("enum_wrong_case", "screen_viewed", _set("properties.screen", "Home"), "invalid_enum: properties.screen"),
    ("integer_below_minimum", "page_turned", _set("properties.spread_number", 0), "out_of_range: properties.spread_number"),
    ("integer_above_maximum", "reader_closed", _set("properties.forward_turns", 100), "out_of_range: properties.forward_turns"),
    ("negative_integer", "catalog_loaded", _set("properties.missing_cover_count", -1), "out_of_range: properties.missing_cover_count"),
    ("book_id_uppercase", "book_opened", _set("properties.book_id", "O-Quintal"), "pattern_mismatch: properties.book_id"),
    ("book_id_with_space", "book_opened", _set("properties.book_id", "o quintal da nuna"), "pattern_mismatch: properties.book_id"),
    ("book_id_trailing_newline", "book_opened", _set("properties.book_id", "o-quintal\n"), "pattern_mismatch: properties.book_id"),
    ("book_id_empty", "book_opened", _set("properties.book_id", ""), "pattern_mismatch: properties.book_id"),
    ("book_id_too_long", "book_opened", _set("properties.book_id", "a" * 65), "pattern_mismatch: properties.book_id"),
    ("book_id_accent", "book_opened", _set("properties.book_id", "a-lua-da-nuná"), "pattern_mismatch: properties.book_id"),
    # Contexto do lote: o motivo leva "context." e vale para o lote todo
    ("unknown_context_field", "app_opened", _set_batch("idfv", "E621E1F8-C36C-495A-93FC-0C247A3E6E5F"), "unknown_property: context.idfv"),
    ("context_device_model", "app_opened", _set_batch("device_model", "iPhone18,1"), "unknown_property: context.device_model"),
    ("missing_session_id", "app_opened", _delete_batch("session_id"), "missing_required: context.session_id"),
    ("session_id_not_uuid", "app_opened", _set_batch("session_id", "abc"), "pattern_mismatch: context.session_id"),
    ("session_id_uuid_v1", "app_opened", _set_batch("session_id", "6fa459ea-ee8a-3ca4-894e-db77e160355e"), "pattern_mismatch: context.session_id"),
    ("session_id_uppercase", "app_opened", _set_batch("session_id", "5D2C8F7A-1B3E-4C6D-8E9F-0A1B2C3D4E5F"), "pattern_mismatch: context.session_id"),
    ("null_session_id", "app_opened", _set_batch("session_id", None), "null_value: context.session_id"),
    ("invalid_device_family", "app_opened", _set_batch("device_family", "iPhone"), "invalid_enum: context.device_family"),
    ("app_version_with_text", "app_opened", _set_batch("app_version", "1.0 beta"), "pattern_mismatch: context.app_version"),
    ("build_with_text", "app_opened", _set_batch("build", "42a"), "pattern_mismatch: context.build"),
    ("os_version_with_name", "app_opened", _set_batch("os_version", "iOS 27.0"), "pattern_mismatch: context.os_version"),
    # Contexto que muda dentro da sessão: vai em cada evento, sem prefixo
    ("missing_event_id", "app_opened", _delete("event_id"), "missing_required: event_id"),
    ("event_id_uppercase", "app_opened", _set("event_id", "C9D8E7F6-A5B4-4C3D-B2E1-F0A9B8C7D604"), "pattern_mismatch: event_id"),
    ("invalid_layout", "app_opened", _set("layout", "double"), "invalid_enum: layout"),
    ("null_layout", "app_opened", _set("layout", None), "null_value: layout"),
    ("missing_subscription_state", "app_opened", _delete("subscription_state"), "missing_required: subscription_state"),
    ("invalid_subscription_state", "app_opened", _set("subscription_state", "trial"), "invalid_enum: subscription_state"),
    ("missing_timestamp", "app_opened", _delete("timestamp"), "missing_required: timestamp"),
    ("timestamp_with_offset", "app_opened", _set("timestamp", "2026-09-17T10:00:00-03:00"), "pattern_mismatch: timestamp"),
    ("timestamp_without_z", "app_opened", _set("timestamp", "2026-09-17T10:00:00"), "pattern_mismatch: timestamp"),
    ("timestamp_microseconds", "app_opened", _set("timestamp", "2026-09-17T10:00:00.123456Z"), "pattern_mismatch: timestamp"),
    ("timestamp_impossible_date", "app_opened", _set("timestamp", "2026-02-30T10:00:00.000Z"), "invalid_timestamp: timestamp"),
    ("timestamp_impossible_hour", "app_opened", _set("timestamp", "2026-09-17T25:00:00.000Z"), "invalid_timestamp: timestamp"),
    ("timestamp_far_future", "app_opened", _set("timestamp", now_ts(timedelta(days=2))), "timestamp_out_of_window: timestamp"),
    ("timestamp_too_old", "app_opened", _set("timestamp", now_ts(timedelta(days=-31))), "timestamp_out_of_window: timestamp"),
]


@pytest.mark.parametrize("base,mutate,reason", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_rejection_reason(client, catalog, base, mutate, reason):
    event = make_event(catalog, base)
    mutate(event)
    response = post_batch(client, [event])
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0, "rejected": [{"index": 0, "reason": reason}]}


@pytest.mark.parametrize("item", [None, 1, "app_opened", ["app_opened"], True])
def test_event_that_is_not_an_object(client, item):
    response = post_batch(client, [item])
    assert response.json()["rejected"] == [{"index": 0, "reason": "event_not_object: event"}]


def test_reason_never_echoes_the_value(client, catalog):
    secret = "Maria search text 555-0100"
    event = make_event(catalog, "screen_viewed", {"screen": secret, "trigger": "initial"})
    response = post_batch(client, [event])
    assert secret not in response.text


def test_batch_context_error_rejects_every_event_in_the_batch(client, catalog):
    events = [
        make_event(catalog, "app_opened", device_family="iPhone"),
        make_event(catalog, "screen_viewed", device_family="iPhone"),
    ]
    response = post_batch(client, events)
    assert response.json() == {
        "accepted": 0,
        "duplicates": 0,
        "rejected": [
            {"index": 0, "reason": "invalid_enum: context.device_family"},
            {"index": 1, "reason": "invalid_enum: context.device_family"},
        ],
    }


def test_small_clock_skew_is_tolerated(client, catalog):
    ahead = make_event(catalog, "app_opened", timestamp=now_ts(timedelta(hours=12)))
    behind = make_event(catalog, "app_opened", timestamp=now_ts(timedelta(days=-7)))
    assert post_batch(client, [ahead, behind]).json()["accepted"] == 2


def test_rejected_events_are_not_logged_with_values(client, catalog, caplog):
    caplog.set_level("DEBUG")
    secret = "Joaozinho"
    event = make_event(catalog, "app_opened")
    event["properties"][secret] = secret
    post_batch(client, [event])
    assert secret not in caplog.text
    assert "unknown_property" in caplog.text


# Validação direta, sem HTTP -----------------------------------------------


def test_validate_event_returns_clean_values(catalog):
    now = datetime.now(timezone.utc)
    event = wire(make_event(catalog, "page_turned", layout="spread"))
    context = catalog.validate_batch_context(make_batch_context(device_family="pad"))
    valid = catalog.validate_event(event, context, now, timedelta(days=30), timedelta(hours=24))
    assert valid.name == "page_turned"
    assert valid.event_id == event["event_id"]
    assert valid.session_id == context["session_id"]
    assert valid.device_family == "pad"
    assert valid.layout == "spread"
    assert valid.subscription_state == "free"
    assert valid.ts.endswith("Z") and len(valid.ts) == 24
    assert json.loads(valid.properties_json()) == event["properties"]


def test_batch_context_is_validated_on_its_own(catalog):
    with pytest.raises(EventRejected) as info:
        catalog.validate_batch_context(make_batch_context(os_version="iOS 27.0"))
    assert info.value.reason == "pattern_mismatch: context.os_version"
    for bad, reason in ((None, "null_value: context"), ([], "invalid_type: context")):
        with pytest.raises(EventRejected) as info:
            catalog.validate_batch_context(bad)
        assert info.value.reason == reason


def test_first_error_wins_and_is_deterministic(catalog):
    now = datetime.now(timezone.utc)
    event = wire(make_event(catalog, "page_turned"))
    event["properties"]["zzz"] = 1
    event["properties"]["aaa"] = 1
    del event["properties"]["book_id"]
    context = catalog.validate_batch_context(make_batch_context())
    with pytest.raises(EventRejected) as info:
        catalog.validate_event(event, context, now, timedelta(days=30), timedelta(hours=24))
    assert info.value.reason == "unknown_property: properties.aaa"


# Integridade do próprio catálogo -------------------------------------------


def test_catalog_file_is_version_1_with_38_events(catalog):
    assert catalog.version == 1
    assert len(catalog.events) == 38
    assert list(catalog.context) == [
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
    ]


def test_catalog_has_no_free_text_or_unbounded_integers(catalog):
    for spec in list(catalog.context.values()) + [
        p for e in catalog.events.values() for p in e.properties.values()
    ]:
        if spec.type == "string":
            assert spec.enum is not None or spec.pattern is not None, spec.name
        if spec.type == "integer":
            assert spec.minimum is not None and spec.maximum is not None, spec.name


def _minimal_catalog():
    with open(str(DEFAULT_CATALOG_PATH), encoding="utf-8") as handle:
        context = json.load(handle)["context"]
    return {
        "version": 1,
        "context": context,
        "events": {"x_happened": {"description": "", "properties": {"ok": {"type": "boolean", "required": True}}}},
    }


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "string", "required": True},  # texto livre
        {"type": "integer", "required": True},  # sem limites
        {"type": "integer", "required": True, "minimum": 5, "maximum": 1},
        {"type": "number", "required": True, "minimum": 0, "maximum": 1},
        {"type": "string", "required": True, "pattern": "[a-z]+"},  # sem âncoras
        {"type": "string", "required": True, "enum": []},
        {"type": "boolean", "requried": True},  # chave com erro de digitação
        {"type": "boolean", "required": "yes"},
    ],
)
def test_catalog_loader_rejects_unsafe_specs(prop):
    data = _minimal_catalog()
    data["events"]["x_happened"]["properties"]["bad"] = prop
    with pytest.raises(CatalogError):
        Catalog.from_dict(data)


def test_catalog_loader_rejects_context_that_does_not_match_the_table():
    data = _minimal_catalog()
    data["context"]["idfv"] = {"type": "string", "required": False, "pattern": "^.*$"}
    with pytest.raises(CatalogError):
        Catalog.from_dict(data)


def test_events_md_mirrors_the_catalog(catalog):
    text = (Path(__file__).resolve().parent.parent / "EVENTS.md").read_text("utf-8")
    documented = [line[len("### `"):-1] for line in text.splitlines() if line.startswith("### `")]
    assert documented == catalog.event_names
    sections = text.split("### `")[1:]
    for section in sections:
        name = section.split("`", 1)[0]
        for prop in catalog.events[name].properties:
            assert "| `%s` |" % prop in section, (name, prop)
