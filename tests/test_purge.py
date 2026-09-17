"""Retenção: python -m app.purge e a limpeza agendada do serviço."""

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import format_timestamp
from app.main import create_app
from app.purge import PurgeScheduler, main, purge

from .helpers import admin_headers, make_event, post_batch

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _count(db_path, sql="SELECT COUNT(*) FROM events"):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _seed(client, catalog, ages_in_days):
    events = [
        make_event(catalog, "app_opened", timestamp=format_timestamp(NOW - timedelta(days=age)))
        for age in ages_in_days
    ]
    assert post_batch(client, events).json()["accepted"] == len(events)
    return events


@pytest.fixture
def wide_settings(settings):
    # Aceita timestamps antigos para simular dados que chegaram há muito tempo.
    return replace(settings, max_event_age_days=36500)


@pytest.fixture
def wide_client(wide_settings, catalog):
    return TestClient(create_app(wide_settings, catalog))


def test_purge_deletes_only_old_events_and_keeps_daily_totals(wide_client, wide_settings, catalog):
    _seed(wide_client, catalog, [400, 200, 200, 181, 179, 10, 0])
    result = purge(wide_settings.db_path, days=180, now=NOW)
    assert result.deleted == 4
    assert result.cutoff == "2026-03-21T12:00:00.000Z"
    assert _count(wide_settings.db_path) == 3

    conn = sqlite3.connect(wide_settings.db_path)
    try:
        totals = conn.execute("SELECT day, name, events FROM daily_event_counts ORDER BY day").fetchall()
        columns = [r[1] for r in conn.execute("PRAGMA table_info(daily_event_counts)")]
    finally:
        conn.close()
    day_200 = (NOW - timedelta(days=200)).strftime("%Y-%m-%d")
    assert (day_200, "app_opened", 2) in totals
    assert sum(r[2] for r in totals) == 4
    assert "session_id" not in columns

    # Rodar de novo não apaga nem soma nada.
    again = purge(wide_settings.db_path, days=180, now=NOW)
    assert again.deleted == 0
    assert _count(wide_settings.db_path, "SELECT SUM(events) FROM daily_event_counts") == 4


def test_purge_aggregates_accumulate_across_runs(wide_client, wide_settings, catalog):
    _seed(wide_client, catalog, [200])
    purge(wide_settings.db_path, days=180, now=NOW)
    # Mesmo dia chegando depois (fila atrasada) soma no total existente.
    _seed(wide_client, catalog, [200])
    purge(wide_settings.db_path, days=180, now=NOW)
    assert _count(wide_settings.db_path, "SELECT events FROM daily_event_counts") == 2


def test_purge_uses_received_day_even_with_wrong_client_clock(client, settings, catalog):
    # Relógio do aparelho "no futuro" não protege o evento da retenção.
    _seed(client, catalog, [0])
    result = purge(settings.db_path, days=1, now=datetime.now(timezone.utc) + timedelta(days=3))
    assert result.deleted == 1


def test_purge_dry_run_deletes_nothing(wide_client, wide_settings, catalog):
    _seed(wide_client, catalog, [300, 1])
    result = purge(wide_settings.db_path, days=180, now=NOW, dry_run=True)
    assert result.dry_run and result.deleted == 1
    assert _count(wide_settings.db_path) == 2
    assert _count(wide_settings.db_path, "SELECT COUNT(*) FROM daily_event_counts") == 0


def test_purge_rejects_invalid_days(settings):
    with pytest.raises(ValueError):
        purge(settings.db_path, days=0)


def test_purged_events_disappear_from_stats(wide_client, wide_settings, catalog):
    _seed(wide_client, catalog, [200])
    day = (NOW - timedelta(days=200)).strftime("%Y-%m-%d")
    params = {"from": day, "to": day}
    before = wide_client.get("/v1/stats/overview", params=params, headers=admin_headers()).json()
    assert before["totals"]["events"] == 1
    purge(wide_settings.db_path, days=180, now=NOW)
    after = wide_client.get("/v1/stats/overview", params=params, headers=admin_headers()).json()
    assert after["totals"]["events"] == 0


def test_cli_main_in_process(wide_client, wide_settings, catalog, capsys, monkeypatch):
    _seed(wide_client, catalog, [3650, 0])
    monkeypatch.delenv("NUNA_ADMIN_TOKEN", raising=False)
    assert main(["--days", "180", "--db", wide_settings.db_path]) == 0
    assert "deleted 1 events" in capsys.readouterr().out
    assert _count(wide_settings.db_path) == 1


def test_cli_rejects_zero_days(settings, monkeypatch):
    monkeypatch.delenv("NUNA_ADMIN_TOKEN", raising=False)
    with pytest.raises(SystemExit) as info:
        main(["--days", "0", "--db", settings.db_path])
    assert info.value.code == 2


def test_cli_as_module_reads_env(wide_client, wide_settings, catalog):
    _seed(wide_client, catalog, [3650, 3650, 0])
    env = dict(os.environ)
    env.pop("NUNA_ADMIN_TOKEN", None)
    env["NUNA_DB_PATH"] = wide_settings.db_path
    dry = subprocess.run(
        [sys.executable, "-m", "app.purge", "--days", "180", "--dry-run"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout.startswith("would delete 2 events")
    real = subprocess.run(
        [sys.executable, "-m", "app.purge", "--days", "180"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert real.returncode == 0, real.stderr
    assert real.stdout.startswith("deleted 2 events")
    assert _count(wide_settings.db_path) == 1


def test_service_purges_on_startup(wide_client, wide_settings, catalog):
    _seed(wide_client, catalog, [3650, 0])
    app = create_app(replace(wide_settings, purge_interval_hours=24, retention_days=180), catalog)
    with TestClient(app):
        scheduler = app.state.purge_scheduler
        assert isinstance(scheduler, PurgeScheduler)
        assert scheduler.wait_first_run(timeout=10)
        assert scheduler.last_result is not None and scheduler.last_result.deleted == 1
    assert _count(wide_settings.db_path) == 1


def test_scheduler_disabled_with_zero_interval(settings, catalog):
    app = create_app(settings, catalog)
    with TestClient(app):
        assert app.state.purge_scheduler is None
