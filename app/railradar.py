from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings

BASE_URL = "https://api.railradar.in/v1"


class RailRadarError(Exception):
    """Any failure talking to RailRadar."""


async def _get(settings: Settings, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {settings.railradar_api_key}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=params, headers=headers)

    if resp.status_code == 401:
        raise RailRadarError("Invalid or expired RailRadar API key")
    if resp.status_code == 404:
        raise RailRadarError("Not found - check the train number or date")
    if resp.status_code == 429:
        raise RailRadarError("RailRadar rate limit exceeded")
    if resp.status_code >= 400:
        raise RailRadarError(f"RailRadar returned HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError:
        raise RailRadarError("RailRadar returned a non-JSON response")

    if not body.get("success"):
        message = (body.get("error") or {}).get("message", "upstream reported a failure")
        raise RailRadarError(message)
    return body.get("data") or {}


async def fetch_station_live(settings: Settings) -> dict[str, Any]:
    """Live arrival/departure board for the configured station (Tundla by default)."""
    return await fetch_station_live_for(settings, settings.station)


async def fetch_station_live_for(settings: Settings, code: str) -> dict[str, Any]:
    """Live board for any station code.

    RailRadar keys the MVIS sensor points off this: MTI (Mitawali South Cabin)
    carries a live board, while WSC is a cabin with no scheduled trains and
    returns an empty board - callers must handle that.
    """
    return await _get(
        settings,
        f"/stations/{code}/live",
        {"hours": settings.hours_ahead, "includeIntermediate": "true"},
    )


async def fetch_station_timetable_for(settings: Settings, code: str) -> dict[str, Any]:
    """Full-day timetable for any station - every train through it, halting or not."""
    return await _get(
        settings,
        f"/stations/{code}/trains",
        {"includeIntermediate": "true"},
    )


async def fetch_station_timetable(settings: Settings) -> dict[str, Any]:
    """Full-day timetable for the configured station."""
    return await fetch_station_timetable_for(settings, settings.station)


async def fetch_train_live(settings: Settings, number: str, date: str | None = None) -> dict[str, Any]:
    """Live running status for a single train number."""
    params = {"date": date} if date else None
    return await _get(settings, f"/trains/{number}/live", params)
