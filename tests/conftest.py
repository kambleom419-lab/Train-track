from __future__ import annotations

import pytest

from app import main as app_main
from app.config import get_settings
from app.railradar import RailRadarError


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch):
    monkeypatch.setenv("RAILRADAR_API_KEY", "rr_live_test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_timetable_cache():
    app_main.reset_timetable_cache()
    yield
    app_main.reset_timetable_cache()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any upstream call unless a test overrides it, so tests never hit the API."""

    async def _fail(*args, **kwargs):
        raise RailRadarError("network disabled in tests")

    monkeypatch.setattr(app_main, "fetch_station_live", _fail)
    monkeypatch.setattr(app_main, "fetch_station_timetable", _fail)
    monkeypatch.setattr(app_main, "fetch_station_timetable_for", _fail)
    monkeypatch.setattr(app_main, "fetch_train_live", _fail)
    yield
