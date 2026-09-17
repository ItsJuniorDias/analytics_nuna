"""Chave do app na ingestão e token de admin nas estatísticas."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.main import create_app

from .helpers import ADMIN_TOKEN, APP_KEY_ROTATED, admin_headers, make_event, post_batch

STATS_PATHS = [
    "/v1/stats/overview",
    "/v1/stats/events?name=app_opened",
    "/v1/stats/funnel?steps=app_opened,book_opened",
    "/v1/stats/books",
    "/v1/stats/paywall",
]


def test_ingest_without_key_is_401(client, catalog):
    response = post_batch(client, [make_event(catalog, "app_opened")], key=None)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_app_key"}


@pytest.mark.parametrize("key", ["wrong", "test-app-key-1 ", "TEST-APP-KEY-1", ""])
def test_ingest_with_wrong_key_is_401(client, catalog, key):
    response = post_batch(client, [make_event(catalog, "app_opened")], key=key)
    assert response.status_code == 401


def test_rotated_key_is_accepted(client, catalog):
    response = post_batch(client, [make_event(catalog, "app_opened")], key=APP_KEY_ROTATED)
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_auth_is_checked_before_reading_the_body(client):
    # Sem chave, nem o JSON inválido nem o tamanho chegam a ser avaliados.
    response = client.post(
        "/v1/events", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401


def test_no_app_keys_configured_rejects_everything(settings, catalog):
    client = TestClient(create_app(replace(settings, app_keys=()), catalog))
    response = post_batch(client, [make_event(catalog, "app_opened")], key="")
    assert response.status_code == 401


@pytest.mark.parametrize("path", STATS_PATHS)
def test_stats_without_token_is_401(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", STATS_PATHS)
@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token-0123456789",
        "Basic " + ADMIN_TOKEN,
        ADMIN_TOKEN,
        "Bearer",
        "Bearer " + ADMIN_TOKEN + "x",
    ],
)
def test_stats_with_wrong_token_is_401(client, path, header):
    response = client.get(path, headers={"Authorization": header})
    assert response.status_code == 401


@pytest.mark.parametrize("path", STATS_PATHS)
def test_stats_with_token_is_200(client, path):
    assert client.get(path, headers=admin_headers()).status_code == 200


def test_app_key_does_not_open_stats(client):
    response = client.get("/v1/stats/overview", headers={"X-Nuna-Key": "test-app-key-1"})
    assert response.status_code == 401


def test_stats_disabled_without_admin_token(settings, catalog):
    client = TestClient(create_app(replace(settings, admin_token=None), catalog))
    assert client.get("/v1/stats/overview", headers=admin_headers()).status_code == 401
    assert client.get("/v1/stats/overview", headers={"Authorization": "Bearer "}).status_code == 401


def test_short_admin_token_is_a_config_error():
    with pytest.raises(ConfigError):
        Settings.from_env({"NUNA_ADMIN_TOKEN": "short"})


def test_settings_from_env_parses_values():
    settings = Settings.from_env(
        {
            "NUNA_APP_KEYS": " a , b ,,",
            "NUNA_ADMIN_TOKEN": "x" * 32,
            "NUNA_DB_PATH": "/data/analytics.db",
            "NUNA_RETENTION_DAYS": "90",
            "NUNA_MAX_BATCH": "50",
            "NUNA_CORS_ORIGINS": "https://a.example, https://b.example",
        }
    )
    assert settings.app_keys == ("a", "b")
    assert settings.retention_days == 90
    assert settings.max_batch == 50
    assert settings.db_path == "/data/analytics.db"
    assert settings.cors_origins == ("https://a.example", "https://b.example")
    defaults = Settings.from_env({})
    assert defaults.retention_days == 180
    assert defaults.max_batch == 100
    assert defaults.db_path == "./data/analytics.db"
    assert defaults.admin_token is None
    with pytest.raises(ConfigError):
        Settings.from_env({"NUNA_MAX_BATCH": "abc"})


def test_dashboard_page_is_public_but_has_no_data(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert client.get("/dashboard/dashboard.js").status_code == 200
    assert client.get("/dashboard/dashboard.css").status_code == 200
    assert client.get("/dashboard/..%2Fmain.py").status_code == 404
    assert client.get("/dashboard/main.py").status_code == 404
