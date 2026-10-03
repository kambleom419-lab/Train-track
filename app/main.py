from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_settings
from app.mvis import TrackEntry, TrackList, is_passenger, mti_passage, resolve_train
from app.railradar import (
    RailRadarError,
    fetch_station_live,
    fetch_station_live_for,
    fetch_station_timetable,
    fetch_station_timetable_for,
    fetch_train_live,
)
from app.timetable import (
    load_timetable,
    now_ist,
    rows_from_station_board,
    running_label,
    upcoming,
)

STATIC_DIR = Path(__file__).parent / "static"

# The full-day station timetable and the route list change rarely, so cache them
# for a few hours. This keeps us at a couple of upstream calls per day instead of
# one per click.
TIMETABLE_TTL_SECONDS = 6 * 3600
_timetable_cache: dict[str, dict[str, Any]] = {}


def reset_timetable_cache() -> None:
    _timetable_cache.clear()


app = FastAPI(title="Tundla Junction Live Trains")

# MVIS: the wheel sensor triggers a lookup, and the trains it identifies are
# refreshed on a timer. Kept in-process for the demo; see the README for why a
# single-process deployment is enough here.
_tracklist: TrackList | None = None
_poller: asyncio.Task | None = None

# Live statuses that mean the train has cleared the section and can be rotated
# out of the queue.
_FINISHED_STATUSES = {"departed", "cancelled", "diverted"}


def _tracks() -> TrackList:
    global _tracklist
    if _tracklist is None:
        _tracklist = TrackList(get_settings())
    return _tracklist


async def _ensure_schedule(settings: Settings) -> None:
    """Give the queue a timetable to pre-fill from, once.

    Served from the cached station boards, so this costs no extra upstream calls:
    the same boards already back the route list.
    """
    tracks = _tracks()
    if tracks.schedule_numbers():
        return
    try:
        origin = await _get_board(settings, settings.route_origin)
        destination = await _get_board(settings, settings.route_destination)
    except (RailRadarError, httpx.HTTPError):
        return
    tracks.set_schedule(join_segment_trains(origin, destination))


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Always answer with JSON so the page can show a real message."""
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {exc}"})


def _load_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError:
        raise HTTPException(
            status_code=500,
            detail="Server not configured: create a .env file with RAILRADAR_API_KEY=<your key>",
        )


async def _call(fetch: Callable[[Settings], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    settings = _load_settings()
    try:
        return await fetch(settings)
    except RailRadarError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach RailRadar: {exc}")


def _cached(cache: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    entry = cache.get(key)
    if entry and entry["expires"] > time.monotonic():
        return entry
    return None


def _store(cache: dict[str, dict[str, Any]], key: str, data: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "data": data,
        "expires": time.monotonic() + TIMETABLE_TTL_SECONDS,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache[key] = entry
    return entry


async def _get_board(settings: Settings, code: str) -> dict[str, Any]:
    """Cached full-day board for a station (1 upstream call per 6h per station)."""
    entry = _cached(_timetable_cache, code)
    if entry:
        return entry["data"]
    data = await fetch_station_timetable_for(settings, code)
    _store(_timetable_cache, code, data)
    return data


async def _live_by_number(settings: Settings) -> dict[str, Any]:
    """Live Tundla board keyed by train number. Empty dict if upstream fails."""
    try:
        live = await fetch_station_live(settings)
    except (RailRadarError, httpx.HTTPError):
        return {}
    result: dict[str, Any] = {}
    for entry in live.get("trains") or []:
        number = (entry.get("train") or {}).get("number")
        if number:
            result[number] = entry
    return result


def _live_fields(entry: dict[str, Any] | None) -> dict[str, Any]:
    info = (entry or {}).get("live") or {}
    return {
        "atTundla": entry is not None,
        "status": info.get("type"),
        "delayMinutes": info.get("delayMinutes"),
        "platform": info.get("platform"),
        "expectedArrival": info.get("expectedArrivalTime"),
        "expectedDeparture": info.get("expectedDepartureTime"),
    }


def _stop_minutes(stop: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """Absolute minutes from the journey start, using the day index.

    The station boards carry `arrivalDay`/`departureDay` per stop, so a stop at
    23:57 on day 1 sorts before 00:03 on day 2 - essential for trains crossing
    midnight in either direction.
    """
    for key in keys:
        value = stop.get(key)
        if not value:
            continue
        try:
            hours, minutes = str(value).split(":")[:2]
            day = int(stop.get(f"{key}Day") or 1)
            return day * 1440 + int(hours) * 60 + int(minutes)
        except (TypeError, ValueError):
            return None
    return None


def join_segment_trains(
    origin_board: dict[str, Any], destination_board: dict[str, Any]
) -> list[dict[str, Any]]:
    """Trains using the origin -> destination segment, travelling that way.

    Both boards are full-day station timetables (`includeIntermediate=true`), so
    trains that pass the destination without halting are included. Direction is
    decided by day-aware times: a train is origin -> destination when it calls at
    the origin before the destination (the reverse direction is dropped, even
    across midnight).
    """
    dest_by_number: dict[str, dict[str, Any]] = {}
    for entry in destination_board.get("trains") or []:
        number = (entry.get("train") or {}).get("number")
        if number and number not in dest_by_number:
            dest_by_number[number] = entry

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in origin_board.get("trains") or []:
        train = entry.get("train") or {}
        number = train.get("number")
        if not number or number in seen:
            continue
        dest_entry = dest_by_number.get(number)
        if dest_entry is None:
            continue
        origin_stop = entry.get("stop") or {}
        dest_stop = dest_entry.get("stop") or {}
        origin_abs = _stop_minutes(origin_stop, ("departure", "arrival"))
        dest_abs = _stop_minutes(dest_stop, ("arrival", "departure"))
        if origin_abs is None or dest_abs is None or origin_abs >= dest_abs:
            continue
        seen.add(number)
        rows.append(
            {
                "number": number,
                "name": train.get("name"),
                "type": train.get("type"),
                "running": running_label(train.get("runDays")),
                "departure": origin_stop.get("departure") or origin_stop.get("arrival"),
                "arrival": dest_stop.get("arrival") or dest_stop.get("departure"),
            }
        )
    return rows


async def _rows_for_next(settings: Settings) -> tuple[list, str]:
    """Timetable rows to pick from: the complete station board, else sheet/snapshot."""
    key = settings.station
    entry = _cached(_timetable_cache, key)
    if entry:
        data = entry["data"]
    else:
        try:
            data = await fetch_station_timetable(settings)
        except (RailRadarError, httpx.HTTPError):
            return await load_timetable()
        _store(_timetable_cache, key, data)
    rows = rows_from_station_board(data)
    if rows:
        return rows, "railradar-board"
    return await load_timetable()


async def _route_numbers(settings: Settings) -> set[str]:
    """Train numbers that run the origin -> destination segment. Empty on failure."""
    try:
        origin_board = await _get_board(settings, settings.route_origin)
        destination_board = await _get_board(settings, settings.route_destination)
    except (RailRadarError, httpx.HTTPError):
        return set()
    return {row["number"] for row in join_segment_trains(origin_board, destination_board)}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/live")
async def live(route_only: bool = True) -> dict:
    """Live board for trains at the station, limited to the route set by default.

    `route_only=true` (the default) keeps only trains that run the configured
    Tundla -> Mitawali segment, so the board never shows unrelated traffic.
    """
    settings = _load_settings()
    data = await _call(fetch_station_live)

    numbers = await _route_numbers(settings) if route_only else set()
    trains = data.get("trains") or []
    if route_only and numbers:
        trains = [t for t in trains if (t.get("train") or {}).get("number") in numbers]

    return {
        "success": True,
        "routeOnly": bool(route_only and numbers),
        "route": f"{settings.route_origin}->{settings.route_destination}",
        "data": {**data, "trains": trains, "count": len(trains)},
    }


@app.get("/api/station/trains")
async def station_trains() -> dict:
    """Full-day list of every train through the station (halting and pass-through)."""
    settings = _load_settings()
    key = settings.station
    entry = _cached(_timetable_cache, key)
    if entry:
        return {"success": True, "data": entry["data"], "cached": True, "asOf": entry["fetched_at"]}

    stale = _timetable_cache.get(key)
    try:
        data = await fetch_station_timetable(settings)
    except (RailRadarError, httpx.HTTPError) as exc:
        if stale:
            return {
                "success": True,
                "data": stale["data"],
                "cached": True,
                "stale": True,
                "asOf": stale["fetched_at"],
                "note": str(exc),
            }
        raise HTTPException(status_code=502, detail=str(exc))

    stored = _store(_timetable_cache, key, data)
    return {"success": True, "data": data, "cached": False, "asOf": stored["fetched_at"]}


@app.get("/api/route/trains")
async def route_trains() -> dict:
    """Trains leaving the origin station toward the destination (Tundla -> Mitawali).

    Built by joining the two stations' full-day boards, so trains that pass the
    destination without halting are included. Live Tundla status is merged in.
    """
    settings = _load_settings()
    origin, destination = settings.route_origin, settings.route_destination

    try:
        origin_board = await _get_board(settings, origin)
        destination_board = await _get_board(settings, destination)
    except (RailRadarError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    rows = join_segment_trains(origin_board, destination_board)
    live_lookup = await _live_by_number(settings)
    for row in rows:
        row.update(_live_fields(live_lookup.get(row["number"])))

    return {
        "success": True,
        "from": {"code": origin, "name": (origin_board.get("station") or {}).get("name")},
        "to": {"code": destination, "name": (destination_board.get("station") or {}).get("name")},
        "count": len(rows),
        "trains": rows,
    }


@app.get("/api/route/timetable.csv")
async def route_timetable_csv() -> Response:
    """Download the origin -> destination train list as CSV."""
    settings = _load_settings()
    origin, destination = settings.route_origin, settings.route_destination
    try:
        origin_board = await _get_board(settings, origin)
        destination_board = await _get_board(settings, destination)
    except (RailRadarError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    rows = join_segment_trains(origin_board, destination_board)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Train No.", "Running", "Departure Time", "Name"])
    for row in sorted(rows, key=lambda r: r["departure"]):
        writer.writerow([row["number"], row["running"], row["departure"], row["name"]])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tundla-mitawali-trains.csv"'},
    )


@app.get("/api/timetable.csv")
async def timetable_csv() -> Response:
    """Download the complete timetable (all trains, all days) as CSV."""
    settings = _load_settings()
    rows, _ = await _rows_for_next(settings)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Train No.", "Running", "Departure Time", "Name"])
    for row in sorted(rows, key=lambda r: r.minutes):
        writer.writerow([row.number, row.running, row.departure, row.name])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tundla-trains.csv"'},
    )


@app.get("/api/train/{number}")
async def train(number: str, date: str | None = None) -> dict:
    """Live running status for a single train number."""
    number = number.strip()
    if not number.isdigit():
        raise HTTPException(status_code=400, detail="Train number must be digits, e.g. 12002")
    data = await _call(lambda settings: fetch_train_live(settings, number, date))
    return {"success": True, "data": data}


@app.get("/api/next")
async def next_trains(count: int = 5) -> dict:
    """The train at/before now plus the next ones for today, with Tundla status."""
    if not 1 <= count <= 10:
        raise HTTPException(status_code=400, detail="count must be between 1 and 10")

    settings = _load_settings()
    now = now_ist()
    rows, source = await _rows_for_next(settings)
    picks = upcoming(rows, now, count)
    live_lookup = await _live_by_number(settings)

    trains = []
    for pick in picks:
        entry = live_lookup.get(pick.number)
        row = {
            "number": pick.number,
            "name": pick.name or (entry or {}).get("train", {}).get("name") or "",
            "scheduled": pick.departure,
            "running": pick.running,
        }
        row.update(_live_fields(entry))
        trains.append(row)

    return {
        "success": True,
        "now": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "source": source,
        "count": len(trains),
        "trains": trains,
    }


# ---------------------------------------------------------------------------
# MVIS — wheel-sensor driven tracking
# ---------------------------------------------------------------------------


class MvisEvent(BaseModel):
    """A wheel-sensor reading posted by the MVIS system."""

    axles: int = Field(..., ge=0, description="Counted axles/wheels for the train")
    point: str | None = Field(default=None, description="Sensor/cabin code, e.g. WSC or MTI")
    trainNumber: str | None = Field(default=None, description="Optional known number (skips resolution)")
    time: str | None = Field(default=None, description="Optional HH:MM the train passed")
    simulateNext: bool = Field(
        default=False,
        description="Demo only: resolve against trains not already tracked, so "
                    "repeated presses fill the queue with different trains",
    )


@app.get("/api/mvis/config")
async def mvis_config() -> dict:
    """Everything the UI needs to describe the MVIS setup."""
    settings = _load_settings()
    tracks = _tracks()
    return {
        "success": True,
        "sensorPoints": [p.strip().upper() for p in settings.mvis_sensor_points.split(",") if p.strip()],
        "axleMin": settings.mvis_axle_min,
        "axleMax": settings.mvis_axle_max,
        "pollSeconds": settings.mvis_poll_seconds,
        "trackSize": settings.mvis_track_size,
        "trackTtlSeconds": settings.mvis_track_ttl_seconds,
        "pollerRunning": bool(_poller and not _poller.done()),
        "stats": tracks.stats,
    }


@app.post("/api/mvis/event")
async def mvis_event(event: MvisEvent) -> dict:
    """Handle one wheel-sensor reading.

    Freight (axles below the minimum), duplicate/faulty counts (above the
    maximum), and events where no train can be resolved are all rejected
    without spending an upstream call beyond the board lookup.
    """
    settings = _load_settings()
    tracks = _tracks()
    tracks.stats["events"] += 1
    await _ensure_schedule(settings)

    point = (event.point or settings.mvis_sensor_points.split(",")[0]).strip().upper()

    if not is_passenger(event.axles, settings):
        tracks.stats["rejectedAxles"] += 1
        return {
            "success": True,
            "accepted": False,
            "reason": "axles-out-of-band",
            "axles": event.axles,
            "band": [settings.mvis_axle_min, settings.mvis_axle_max],
        }

    try:
        # A fresh event identifies a train we are not already following, so skip
        # anything on the tracklist. Repeated events for the same physical train
        # are rare because the gate plus the window keeps them out.
        exclude = {e.number for e in tracks.list()} if event.simulateNext else set()
        # Only ever identify trains that run the configured segment - a train
        # that merely passes Tundla on another route is not ours.
        route_numbers = set(tracks.schedule_numbers())
        resolved = await resolve_train(
            settings, point, fetch_station_live_for,
            train_number=event.trainNumber, exclude=exclude,
            route_numbers=route_numbers or None,
        )
        tracks.stats["apiCalls"] += 1
    except (RailRadarError, httpx.HTTPError) as exc:
        tracks.stats["unresolved"] += 1
        raise HTTPException(status_code=502, detail=str(exc))

    if not resolved:
        tracks.stats["unresolved"] += 1
        return {"success": True, "accepted": False, "reason": "no-train-on-board",
                "point": point, "axles": event.axles}

    entry = TrackEntry(
        number=resolved["number"],
        name=resolved.get("name") or "",
        axles=event.axles,
        sensor_point=point,
        identified_by=resolved["identifiedBy"],
        source_time=event.time or resolved.get("sourceTime"),
    )
    tracks.add(entry)
    tracks.stats["accepted"] += 1

    status = await _refresh_entry(settings, entry)
    return {
        "success": True,
        "accepted": True,
        "point": point,
        "axles": event.axles,
        "train": entry.to_dict(),
        "status": status,
        "stats": tracks.stats,
    }


async def _refresh_entry(settings: Settings, entry: TrackEntry) -> dict:
    """One upstream call for a single train's live status.

    Failures are recorded rather than raised so a batch refresh or the poller
    keeps going: one rate-limited train must not stall the others.
    """
    tracks = _tracks()
    try:
        data = await fetch_train_live(settings, entry.number)
        tracks.stats["apiCalls"] += 1
    except (RailRadarError, httpx.HTTPError) as exc:
        tracks.stats["refreshErrors"] += 1
        entry.status = {**entry.status, "error": str(exc)}
        return entry.status

    info = (data.get("currentLocation") or {})
    passage = mti_passage(data, settings.route_destination)

    entry.status = {
        "status": data.get("status") or info.get("status"),
        "delayMinutes": data.get("delayMinutes"),
        "lastStation": (info.get("lastStation") or {}).get("name")
        if isinstance(info.get("lastStation"), dict) else info.get("lastStation"),
        "nextStation": (info.get("nextStation") or {}).get("name")
        if isinstance(info.get("nextStation"), dict) else info.get("nextStation"),
        "lastStationCode": info.get("stationCode"),
        "lastStationName": info.get("stationName"),
        "speedKmh": info.get("speedKmh"),
        "segmentProgress": info.get("segmentProgress"),
        "lastUpdate": data.get("lastUpdatedAt") or data.get("lastUpdate"),
        # position along the Tundla -> Mitawali segment, straight from RailRadar
        "mtiPresent": passage["present"],
        "mtiIndex": passage["index"],
        "trainSequence": passage["sequence"],
        "passedMti": passage["passed"],
        "segmentProgressPct": passage["progress"],
    }
    entry.refreshed_at = time.monotonic()
    # Prefer RailRadar's own "last updated" stamp so the UI timer shows the age
    # of the data we actually received, not the age of our cache entry.
    entry.refreshed_wall = _upstream_epoch(data) or time.time()
    entry.polls += 1
    tracks.stats["lastRefreshAt"] = datetime.now(timezone.utc).isoformat()
    return entry.status


def _upstream_epoch(data: dict[str, Any]) -> float | None:
    """Parse RailRadar's lastUpdatedAt into unix seconds, if present."""
    raw = data.get("lastUpdatedAt") or data.get("lastUpdate")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@app.get("/api/mvis/tracked")
async def mvis_tracked(refresh: bool = False) -> dict:
    """The trains currently being tracked, newest first."""
    settings = _load_settings()
    tracks = _tracks()
    await _ensure_schedule(settings)
    tracks.prune()

    if refresh and tracks.list():
        await asyncio.gather(*(_refresh_entry(settings, e) for e in tracks.list()),
                             return_exceptions=True)

    return {
        "success": True,
        "count": len(tracks.list()),
        "trackSize": settings.mvis_track_size,
        "pollSeconds": settings.mvis_poll_seconds,
        "pollerRunning": bool(_poller and not _poller.done()),
        "focus": (tracks.focus().number if tracks.focus() else None),
        "stats": tracks.stats,
        "trains": [e.to_dict() for e in tracks.list()],
    }


@app.post("/api/mvis/focus")
async def mvis_set_focus(number: str) -> dict:
    """Make one of the cached trains the actively polled one.

    The MVIS system posts the next detection to take over focus; this endpoint
    is for an operator clicking a row to follow it instead.
    """
    tracks = _tracks()
    if not tracks.set_focus(number.strip()):
        raise HTTPException(status_code=404, detail=f"{number} is not being tracked")
    entry = tracks.focus()
    settings = _load_settings()
    await _refresh_entry(settings, entry)          # bring it up to date immediately
    return {"success": True, "focus": entry.number,
            "stats": tracks.stats, "trains": [e.to_dict() for e in tracks.list()]}


@app.post("/api/mvis/rotate")
async def mvis_rotate(number: str) -> dict:
    """Drop a train that has cleared the section and promote the next one.

    The poller calls this automatically when the live train's status becomes
    `departed`; it is exposed so a demo (or the MVIS system) can advance the
    queue on demand.
    """
    settings = _load_settings()
    tracks = _tracks()
    await _ensure_schedule(settings)

    number = number.strip()
    if not tracks.rotate_out(number):
        raise HTTPException(status_code=404, detail=f"{number} is not being tracked")

    promoted = tracks.focus()
    if promoted is not None:
        await _refresh_entry(settings, promoted)     # bring the new live train up to date
    return {"success": True, "removed": number, "focus": promoted.number if promoted else None,
            "count": len(tracks.list()),
            "stats": tracks.stats, "trains": [e.to_dict() for e in tracks.list()]}


@app.post("/api/mvis/poll")
async def mvis_poll_once(refresh_all: bool = False) -> dict:
    """Refresh the focus train once (the 'refresh now' button).

    `refresh_all=true` refreshes every tracked train instead - useful when
    switching focus to a cached entry so its status becomes current again.
    """
    settings = _load_settings()
    tracks = _tracks()
    tracks.prune()
    if refresh_all:
        targets = tracks.list()
    else:
        focus = tracks.focus()
        targets = [focus] if focus else []
    if targets:
        await asyncio.gather(*(_refresh_entry(settings, e) for e in targets),
                             return_exceptions=True)
    return {"success": True, "refreshed": len(targets), "count": len(tracks.list()),
            "focus": tracks.focus().number if tracks.focus() else None,
            "stats": tracks.stats, "trains": [e.to_dict() for e in tracks.list()]}


@app.post("/api/mvis/poller")
async def mvis_set_poller(running: bool = True) -> dict:
    """Start or stop the background refresh loop.

    Each cycle costs one upstream call per tracked train, so it defaults to off
    and is only used when we actively want a live-following demo.
    """
    global _poller
    if running:
        if _poller and not _poller.done():
            return {"success": True, "pollerRunning": True}
        _poller = asyncio.create_task(_poll_loop())
        return {"success": True, "pollerRunning": True}

    if _poller and not _poller.done():
        _poller.cancel()
        try:
            await _poller
        except asyncio.CancelledError:
            pass
    _poller = None
    return {"success": True, "pollerRunning": False}


async def _poll_loop() -> None:
    """Advance the queue every `mvis_poll_seconds` until cancelled.

    Each cycle:
      1. refresh the focus train (one upstream call),
      2. if it has cleared Mitawali, rotate it out of the queue,
      3. promote the next train as live and refresh it so the UI updates,
      4. top the queue back up from the timetable.

    One upstream call per cycle, plus one when the live train changes.
    """
    while True:
        settings = _load_settings()
        tracks = _tracks()
        tracks.prune()

        entry = tracks.focus()
        if entry is not None:
            await _refresh_entry(settings, entry)

            # RailRadar tells us the train's position in its route, so we know
            # exactly when it has gone past Mitawali and left our section.
            cleared = entry.status.get("passedMti") or \
                entry.status.get("status") in _FINISHED_STATUSES
            if cleared:
                tracks.rotate_out(entry.number)
                nxt = tracks.focus()
                if nxt is not None:
                    await _refresh_entry(settings, nxt)

        try:
            await asyncio.sleep(settings.mvis_poll_seconds)
        except asyncio.CancelledError:
            raise


@app.on_event("shutdown")
async def _stop_poller() -> None:
    if _poller and not _poller.done():
        _poller.cancel()
