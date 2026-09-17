"""POST /v1/events: lote válido, idempotência, limites e envelope."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .helpers import APP_KEY, batch_body, make_batch_context, make_event, new_uuid, now_ts, post_batch, sample_properties

# Envelope mínimo válido para os testes que montam o corpo à mão.
EMPTY = json.dumps({"context": make_batch_context(), "events": []}).encode("utf-8")


def rows(db_path, sql="SELECT * FROM events"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_every_catalog_event_is_accepted(client, catalog, settings):
    events = [make_event(catalog, name) for name in catalog.event_names]
    events += [
        make_event(catalog, name, sample_properties(catalog, name, include_optional=False))
        for name in catalog.event_names
    ]
    assert len(events) <= settings.max_batch
    response = post_batch(client, events)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": len(events), "duplicates": 0, "rejected": []}
    assert len(rows(settings.db_path)) == len(events)


def test_stored_row_is_normalized(client, catalog, settings):
    client_ts = now_ts()
    session = new_uuid()
    event = make_event(
        catalog,
        "book_opened",
        # ".5" é meio segundo e precisa virar ".500" no banco.
        timestamp=client_ts[:19] + ".5Z",
        layout="spread",
        subscription_state="premium",
        session_id=session,
        device_family="pad",
    )
    response = post_batch(client, [event])
    assert response.json()["accepted"] == 1

    (row,) = rows(settings.db_path)
    assert row["event_id"] == event["event_id"]
    assert row["session_id"] == session
    assert row["name"] == "book_opened"
    assert row["ts"] == client_ts[:19] + ".500Z"
    assert len(row["received_at"]) == 10  # só o dia, de propósito
    assert row["catalog_version"] == 1
    assert row["layout"] == "spread"
    assert row["subscription_state"] == "premium"
    assert row["device_family"] == "pad"
    assert json.loads(row["properties"]) == event["properties"]


def test_layout_is_optional_and_stored_as_null(client, catalog, settings):
    post_batch(client, [make_event(catalog, "app_opened")])
    (row,) = rows(settings.db_path)
    assert row["layout"] is None


def test_timestamp_without_fraction_is_accepted(client, catalog, settings):
    event = make_event(catalog, "app_opened")
    event["timestamp"] = now_ts()[:19] + "Z"
    assert post_batch(client, [event]).json()["accepted"] == 1
    (row,) = rows(settings.db_path)
    assert row["ts"].endswith(".000Z")


def test_resending_the_same_batch_is_idempotent(client, catalog, settings):
    events = [make_event(catalog, "app_opened"), make_event(catalog, "screen_viewed")]
    first = post_batch(client, events)
    assert first.json() == {"accepted": 2, "duplicates": 0, "rejected": []}
    second = post_batch(client, events)
    assert second.status_code == 200
    assert second.json() == {"accepted": 0, "duplicates": 2, "rejected": []}
    assert len(rows(settings.db_path)) == 2


def test_duplicate_inside_one_batch_and_first_copy_wins(client, catalog, settings):
    event = make_event(catalog, "app_opened", {"onboarding_completed": True})
    copy = json.loads(json.dumps(event))
    copy["properties"]["onboarding_completed"] = False
    response = post_batch(client, [event, copy])
    assert response.json() == {"accepted": 1, "duplicates": 1, "rejected": []}
    (row,) = rows(settings.db_path)
    assert json.loads(row["properties"]) == {"onboarding_completed": True}


def test_partial_batch_reports_rejected_indexes(client, catalog, settings):
    good = make_event(catalog, "app_opened")
    bad_enum = make_event(catalog, "screen_viewed", {"screen": "settings", "trigger": "initial"})
    also_good = make_event(catalog, "app_backgrounded")
    unknown = make_event(catalog, "app_opened")
    unknown["name"] = "app_crashed"
    response = post_batch(client, [good, bad_enum, also_good, unknown])
    assert response.status_code == 200
    assert response.json() == {
        "accepted": 2,
        "duplicates": 0,
        "rejected": [
            {"index": 1, "reason": "invalid_enum: properties.screen"},
            {"index": 3, "reason": "unknown_event: name"},
        ],
    }
    assert sorted(r["name"] for r in rows(settings.db_path)) == ["app_backgrounded", "app_opened"]


def test_rejected_event_does_not_consume_its_event_id(client, catalog, settings):
    event = make_event(catalog, "app_opened", {"onboarding_completed": "yes"})
    assert post_batch(client, [event]).json()["rejected"][0]["index"] == 0
    event["properties"]["onboarding_completed"] = True
    assert post_batch(client, [event]).json()["accepted"] == 1


def test_empty_batch_is_ok(client):
    response = post_batch(client, [])
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0, "rejected": []}


def test_batch_limit(settings, catalog):
    client = TestClient(create_app(replace(settings, max_batch=5), catalog))
    at_limit = [make_event(catalog, "app_opened") for _ in range(5)]
    assert post_batch(client, at_limit).json()["accepted"] == 5

    over = [make_event(catalog, "app_opened") for _ in range(6)]
    response = post_batch(client, over)
    assert response.status_code == 413
    assert response.json() == {"detail": "batch_too_large"}
    # Nada do lote grande foi gravado.
    assert len(rows(settings.db_path)) == 5


def test_default_batch_limit_is_100(client, catalog):
    over = [make_event(catalog, "app_opened") for _ in range(101)]
    assert post_batch(client, over).status_code == 413


def test_body_size_limit_with_content_length(settings, catalog):
    client = TestClient(create_app(replace(settings, max_body_bytes=2048), catalog))
    events = [make_event(catalog, "reader_closed") for _ in range(10)]
    response = post_batch(client, events)
    assert response.status_code == 413
    assert response.json() == {"detail": "body_too_large"}


def test_body_size_limit_without_content_length(settings, catalog):
    client = TestClient(create_app(replace(settings, max_body_bytes=2048), catalog))
    payload = json.dumps(batch_body([make_event(catalog, "reader_closed") for _ in range(10)]))

    def chunks():
        data = payload.encode("utf-8")
        for i in range(0, len(data), 500):
            yield data[i : i + 500]

    response = client.post(
        "/v1/events",
        content=chunks(),
        headers={"X-Nuna-Key": APP_KEY, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_default_body_limit_is_256kb(client):
    body = b'{"catalog_version": 1, "events": [], "pad": "' + b"x" * (256 * 1024) + b'"}'
    response = client.post(
        "/v1/events", content=body, headers={"X-Nuna-Key": APP_KEY, "Content-Type": "application/json"}
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b'{"catalog_version": 1, "events": [NaN]}',
        b'{"catalog_version": 1, "events": [Infinity]}',
        b'{"catalog_version": 1, "catalog_version": 1, "events": []}',
        b"\xff\xfe",
        b"[" * 5000 + b"]" * 5000,
    ],
)
def test_invalid_json_is_400(client, body):
    response = client.post(
        "/v1/events", content=body, headers={"X-Nuna-Key": APP_KEY, "Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_json"}


def test_wrong_content_type_is_415(client):
    response = client.post(
        "/v1/events",
        content=EMPTY,
        headers={"X-Nuna-Key": APP_KEY, "Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_content_type_with_charset_is_fine(client):
    response = client.post(
        "/v1/events",
        content=EMPTY,
        headers={"X-Nuna-Key": APP_KEY, "Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"events": []},
        {"context": make_batch_context()},
        {"context": None, "events": []},
        {"context": [], "events": []},
        {"context": "phone", "events": []},
        {"context": make_batch_context(), "events": {}},
        {"context": make_batch_context(), "catalog_version": "1", "events": []},
        {"context": make_batch_context(), "catalog_version": True, "events": []},
        # O formato antigo, com contexto dentro de cada evento, não passa.
        {"catalog_version": 1, "events": []},
        {"context": make_batch_context(), "events": [], "session_id": "x"},
    ],
)
def test_invalid_envelope_is_422(client, body):
    response = post_batch(client, [], body=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_envelope"}


def test_unsupported_catalog_version_is_422(client, catalog):
    body = batch_body([make_event(catalog, "app_opened")])
    body["catalog_version"] = 2
    response = post_batch(client, [], body=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported_catalog_version"}


def test_catalog_version_is_optional_and_1_is_accepted(client, catalog):
    # O app não manda catalog_version: ausente vale 1.
    without = batch_body([make_event(catalog, "app_opened")])
    assert "catalog_version" not in without
    assert post_batch(client, [], body=without).json()["accepted"] == 1
    explicit = batch_body([make_event(catalog, "app_opened")])
    explicit["catalog_version"] = 1
    assert post_batch(client, [], body=explicit).json()["accepted"] == 1


def test_layout_can_be_omitted_or_sent_per_event(client, catalog, settings):
    # O app omite layout fora do leitor (nunca manda null) e manda por evento,
    # porque dobrar o Duo muda o layout no meio da sessão.
    events = [
        make_event(catalog, "app_opened"),
        make_event(catalog, "page_turned", layout="single"),
        make_event(catalog, "page_turned", layout="spread"),
    ]
    assert post_batch(client, events).json() == {"accepted": 3, "duplicates": 0, "rejected": []}
    layouts = sorted((r["layout"] or "") for r in rows(settings.db_path))
    assert layouts == ["", "single", "spread"]


def test_one_upload_per_batch_context(client, catalog, settings):
    # Mesmo aparelho, duas sessões na fila: o app manda dois lotes.
    first, second = new_uuid(), new_uuid()
    events = [
        make_event(catalog, "app_opened", session_id=first),
        make_event(catalog, "app_opened", session_id=second),
    ]
    assert post_batch(client, events).json()["accepted"] == 2
    assert sorted(r["session_id"] for r in rows(settings.db_path)) == sorted([first, second])


def test_health_and_catalog_endpoints(client, catalog):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "catalog_version": 1}
    published = client.get("/v1/catalog")
    assert published.status_code == 200
    assert published.json() == catalog.raw


def test_docs_are_off_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_example_batch_file_is_valid(make_client):
    # examples/batch.json é o contrato mostrado no README; se sair do catálogo,
    # este teste quebra.
    client = make_client(max_event_age_days=36500)
    body = json.loads((Path(__file__).resolve().parent.parent / "examples" / "batch.json").read_text("utf-8"))
    response = post_batch(client, [], body=body)
    assert response.status_code == 200, response.text
    assert response.json()["rejected"] == []
    assert response.json()["accepted"] == len(body["events"])


def test_event_ids_are_uuid_v4_lowercase(client, catalog):
    upper = make_event(catalog, "app_opened", event_id=new_uuid().upper())
    response = post_batch(client, [upper])
    assert response.json()["rejected"] == [{"index": 0, "reason": "pattern_mismatch: event_id"}]
