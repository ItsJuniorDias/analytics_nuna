"""IP, User-Agent e identificadores nunca chegam ao banco, aos logs ou ao código."""

import re
import sqlite3
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient

from app.main import create_app

from .helpers import APP_KEY, make_event, new_uuid, post_batch

ROOT = Path(__file__).resolve().parent.parent
# Comparação por pedaço do nome (separado por "_"): "subscription" contém "ip"
# como texto, mas não é uma coluna de IP.
FORBIDDEN_TOKENS = {
    "ip", "ipv4", "ipv6", "addr", "address", "agent", "ua", "forwarded", "remote", "client",
    "host", "idfa", "idfv", "advertising", "install", "user", "account", "model", "location",
    "geo", "country", "city", "timezone", "tz", "locale", "token", "email", "query", "text",
}

SPY_IP = "203.0.113.77"
SPY_IPV6 = "2001:db8::77"
SPY_UA = "Nuna/1.0 CFNetwork/9999 Darwin/27.0 SpyAgent"


def _all_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)] for t in tables}
    finally:
        conn.close()


def _dump(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _post_with_spy_headers(client, catalog):
    return post_batch(
        client,
        [make_event(catalog, "app_opened"), make_event(catalog, "book_opened")],
        headers={
            "User-Agent": SPY_UA,
            "X-Forwarded-For": SPY_IP + ", 10.0.0.1",
            "X-Real-IP": SPY_IP,
            "Forwarded": "for=%s;proto=https" % SPY_IP,
            "CF-Connecting-IP": SPY_IPV6,
            "True-Client-IP": SPY_IP,
        },
    )


def test_schema_has_no_ip_or_user_agent_columns(client, catalog, settings):
    assert _post_with_spy_headers(client, catalog).status_code == 200
    columns = _all_columns(settings.db_path)
    assert set(columns) == {"events", "daily_event_counts", "schema_migrations"}
    for table, names in columns.items():
        for name in names:
            assert not (set(name.lower().split("_")) & FORBIDDEN_TOKENS), "%s.%s" % (table, name)
    assert columns["events"] == [
        "event_id",
        "name",
        "session_id",
        "ts",
        "received_at",
        "catalog_version",
        "app_version",
        "build",
        "os_version",
        "device_family",
        "layout",
        "subscription_state",
        "properties",
        # migração 2: país da conta da App Store (ISO alfa-3), não localização
        "storefront",
    ]


def test_nothing_from_the_connection_is_persisted(client, catalog, settings):
    assert _post_with_spy_headers(client, catalog).json()["accepted"] == 2
    dump = _dump(settings.db_path)
    for needle in (SPY_IP, SPY_IPV6, "10.0.0.1", "SpyAgent", "CFNetwork", "testclient", APP_KEY):
        assert needle not in dump, needle


def test_received_at_is_day_only(client, catalog, settings):
    _post_with_spy_headers(client, catalog)
    conn = sqlite3.connect(settings.db_path)
    try:
        values = {r[0] for r in conn.execute("SELECT received_at FROM events")}
    finally:
        conn.close()
    assert len(values) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", values.pop())


def test_events_table_has_no_insertion_order(settings, client, catalog):
    # WITHOUT ROWID: não existe rowid sequencial que revele a ordem de chegada.
    _post_with_spy_headers(client, catalog)
    conn = sqlite3.connect(settings.db_path)
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'events'").fetchone()[0]
        assert "WITHOUT ROWID" in sql.upper()
        try:
            conn.execute("SELECT rowid FROM events").fetchall()
            has_rowid = True
        except sqlite3.OperationalError:
            has_rowid = False
    finally:
        conn.close()
    assert has_rowid is False


def test_logs_do_not_contain_ip_or_user_agent(client, catalog, caplog):
    caplog.set_level("DEBUG")
    _post_with_spy_headers(client, catalog)
    bad = make_event(catalog, "app_opened")
    bad["properties"]["extra"] = SPY_IP
    post_batch(client, [bad], headers={"User-Agent": SPY_UA, "X-Forwarded-For": SPY_IP})
    for needle in (SPY_IP, SPY_IPV6, "SpyAgent", "testclient"):
        assert needle not in caplog.text


def test_application_never_sees_client_address_or_identifying_headers(settings, catalog):
    app = create_app(settings, catalog)
    seen = {}

    @app.get("/__probe")
    def probe(request: Request):
        seen["client"] = request.client
        seen["headers"] = dict(request.headers)
        return {}

    client = TestClient(app)
    client.get(
        "/__probe",
        headers={
            "User-Agent": SPY_UA,
            "X-Forwarded-For": SPY_IP,
            "X-Real-IP": SPY_IP,
            "Forwarded": "for=" + SPY_IP,
            "CF-Connecting-IP": SPY_IP,
            "Cookie": "a=b",
            "Referer": "https://example.com/",
            "X-Nuna-Key": APP_KEY,
        },
    )
    assert seen["client"] is None
    lowered = {k.lower() for k in seen["headers"]}
    for header in ("user-agent", "x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "cookie", "referer"):
        assert header not in lowered
    # O que a aplicação precisa continua chegando.
    assert seen["headers"]["x-nuna-key"] == APP_KEY
    assert SPY_IP not in repr(seen)


def test_source_code_never_reads_client_address():
    # Procura uso em código (atributos e literais entre aspas), não menções em
    # comentários que explicam por que esses dados são descartados.
    code_patterns = [
        re.compile(r"request\.client\b"),
        re.compile(r"\.client\.host\b"),
        re.compile(r"""scope\[["']client["']\]"""),
        re.compile(r"""["'](user-agent|x-forwarded-for|x-real-ip|forwarded|cf-connecting-ip)["']""", re.I),
        re.compile(r"remote_addr", re.I),
    ]
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name == "privacy.py":
            continue  # é o módulo que apaga esses dados
        text = path.read_text("utf-8")
        for pattern in code_patterns:
            if pattern.search(text):
                offenders.append("%s: %s" % (path.name, pattern.pattern))
    assert offenders == []


def test_every_run_command_disables_access_log_and_proxy_headers():
    for name in ("Dockerfile", "Makefile", "render.yaml", "docker-compose.yml"):
        text = (ROOT / name).read_text("utf-8")
        if "uvicorn" in text:
            assert "--no-access-log" in text, name
            assert "--no-proxy-headers" in text, name
    main_module = (ROOT / "app" / "__main__.py").read_text("utf-8")
    assert "access_log=False" in main_module
    assert "proxy_headers=False" in main_module


def test_stats_never_return_session_ids(client, catalog):
    from .helpers import admin_headers

    session = new_uuid()
    events = [
        make_event(catalog, name, session_id=session)
        for name in ("app_opened", "paywall_viewed", "book_opened", "book_completed")
    ]
    post_batch(client, events)
    session_ids = {session}
    event_ids = {e["event_id"] for e in events}
    # A lista de sessões devolve linhas individuais, mas sem id: a checagem
    # abaixo só prova alguma coisa se a sessão estiver mesmo na resposta.
    listed = client.get("/v1/stats/paywall/sessions", headers=admin_headers()).json()
    assert listed["total"] == 1 and listed["sessions"][0]["event_count"] == len(events)
    for path in (
        "/v1/stats/overview",
        "/v1/stats/events?name=book_opened&by=book_id",
        "/v1/stats/funnel?steps=app_opened,book_opened",
        "/v1/stats/books",
        "/v1/stats/paywall",
        "/v1/stats/paywall/conversion?by=source",
        "/v1/stats/paywall/sessions",
    ):
        text = client.get(path, headers=admin_headers()).text
        for value in session_ids | event_ids:
            assert value not in text, path


def test_session_and_event_ids_cannot_be_used_as_breakdown(client, catalog):
    from .helpers import admin_headers

    for field in ("session_id", "event_id", "timestamp"):
        response = client.get("/v1/stats/events?name=app_opened&by=" + field, headers=admin_headers())
        assert response.status_code == 422
