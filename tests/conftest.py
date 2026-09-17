"""Fixtures compartilhadas."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.catalog import Catalog
from app.config import DEFAULT_CATALOG_PATH, Settings
from app.main import create_app

from .helpers import ADMIN_TOKEN, APP_KEY, APP_KEY_ROTATED


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load(DEFAULT_CATALOG_PATH)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_keys=(APP_KEY, APP_KEY_ROTATED),
        admin_token=ADMIN_TOKEN,
        db_path=str(tmp_path / "analytics.db"),
        max_batch=100,
        purge_interval_hours=0,
    )


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> TestClient:
    return TestClient(create_app(settings, catalog))


@pytest.fixture
def make_client(tmp_path: Path, catalog: Catalog):
    """Cliente com settings sob medida (ex.: janela de datas larga para seeds)."""

    def factory(**overrides: Any) -> TestClient:
        base = Settings(
            app_keys=(APP_KEY,),
            admin_token=ADMIN_TOKEN,
            db_path=str(tmp_path / "custom.db"),
            purge_interval_hours=0,
        )
        return TestClient(create_app(replace(base, **overrides), catalog))

    return factory
