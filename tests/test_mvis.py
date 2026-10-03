"""Tests for the MVIS wheel-sensor flow.

No network: every RailRadar call is monkeypatched, so the suite never spends
quota.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, mvis
from app.config import Settings
from app.mvis import (
    TrackEntry,
    TrackList,
    _entry_time,
    is_passenger,
    mti_passage,
    resolve_train,
)


def make_settings(**overrides) -> Settings:
    base = {
        "railradar_api_key": "test-key",
        "station_code": "TDL",
        "mvis_sensor_points": "WSC,MTI",
        "mvis_axle_min": 64,
        "mvis_axle_max": 100,
        "mvis_poll_seconds": 10,
        "mvis_track_size": 5,
        "mvis_track_ttl_seconds": 900,
    }
    base.update(overrides)
    return Settings(**base)


# --- axle rule -------------------------------------------------------------

@pytest.mark.parametrize("axles,expected", [
    (64, True),    # inclusive lower bound
    (72, True),
    (100, True),   # inclusive upper bound
    (63, False),   # goods
    (40, False),
    (101, False),  # duplicate / faulty count
    (140, False),
    (0, False),
])
def test_axle_band_is_inclusive(axles, expected):
    assert is_passenger(axles, make_settings()) is expected


def test_axle_band_respects_config():
    s = make_settings(mvis_axle_min=50, mvis_axle_max=60)
    assert is_passenger(55, s) is True
    assert is_passenger(64, s) is False


# --- resolver --------------------------------------------------------------

def board(number, hhmm, name="Test Express", day=1, key="departure"):
    stop = {key: hhmm, f"{key}Day": day}
    return {"trains": [{"train": {"number": number, "name": name}, "stop": stop}]}


async def _boards(settings, code):
    # Departure times are relative to now so the resolver's "already left"
    # cutoff is exercised the way it is in production.
    return {
        "MTI": board("11111", _hhmm_from_now(-5), name="Via Mitawali"),
        "TDL": {
            "trains": [
                {"train": {"number": "22222", "name": "Older"},
                 "stop": {"departure": _hhmm_from_now(-120), "departureDay": 1}},
                {"train": {"number": "33333", "name": "Newest"},
                 "stop": {"departure": _hhmm_from_now(-5), "departureDay": 1}},
                {"train": {"number": "44444", "name": "Third"},
                 "stop": {"departure": _hhmm_from_now(-8), "departureDay": 1}},
                {"train": {"number": "55555", "name": "Fourth"},
                 "stop": {"departure": _hhmm_from_now(-11), "departureDay": 1}},
                {"train": {"number": "66666", "name": "Fifth"},
                 "stop": {"departure": _hhmm_from_now(-14), "departureDay": 1}},
                {"train": {"number": "77777", "name": "Sixth"},
                 "stop": {"departure": _hhmm_from_now(-17), "departureDay": 1}},
            ]
        },
        "WSC": {"trains": []},          # a cabin: no RailRadar schedule
    }[code]


# The fake board trains above stand in for trains on the TDL->MTI segment, so
# the resolver's route filter must accept them.
ROUTE_NUMBERS = {"11111", "22222", "33333", "44444", "55555", "66666", "77777"}

IST = timezone(timedelta(hours=5, minutes=30))


def _iso_minutes_ago(minutes: int) -> str:
    """ISO timestamp `minutes` in the past, for live.expectedDepartureTime."""
    when = datetime.now(IST) - timedelta(minutes=minutes)
    return when.isoformat()


def _hhmm_from_now(offset_minutes: int) -> str:
    """'HH:MM' for a time `offset_minutes` away from now (may roll past midnight)."""
    when = datetime.now(IST) + timedelta(minutes=offset_minutes)
    return when.strftime("%H:%M")


async def test_resolver_uses_sensor_board_when_it_has_trains():
    s = make_settings()
    got = await resolve_train(s, "MTI", _boards)
    assert got["number"] == "11111"
    assert got["identifiedBy"] == "board:MTI"


async def test_resolver_falls_back_to_tundla_for_empty_cabin():
    s = make_settings()
    got = await resolve_train(s, "WSC", _boards)
    assert got["identifiedBy"] == "fallback:TDL"
    # the most recent departure wins - that is the train that just cleared
    assert got["number"] == "33333"
    assert got["sourceTime"] == _hhmm_from_now(-5)


async def test_resolver_prefers_explicit_number():
    s = make_settings()
    got = await resolve_train(s, "WSC", _boards, train_number="12942")
    assert got["number"] == "12942"
    assert got["identifiedBy"] == "explicit"


async def test_resolver_skips_trains_not_on_our_route():
    """The 15635 bug: a train passing Tundla on another route was picked."""

    async def boards(settings, code):
        return {
            "TDL": {
                "trains": [
                    # newest departure, but NOT on the TDL->MTI segment
                    {"train": {"number": "15635", "name": "Guwahati Dwarka"},
                     "stop": {"departure": _hhmm_from_now(-2), "departureDay": 1}},
                    {"train": {"number": "33333", "name": "Ours"},
                     "stop": {"departure": _hhmm_from_now(-5), "departureDay": 1}},
                ]
            },
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    s = make_settings()
    unrestricted = await resolve_train(s, "WSC", boards)
    assert unrestricted["number"] == "15635"        # old behaviour

    restricted = await resolve_train(s, "WSC", boards, route_numbers={"33333"})
    assert restricted["number"] == "33333"          # what we now do
    assert restricted["identifiedBy"] == "fallback:TDL"


async def test_resolver_ranks_by_actual_departure_not_scheduled():
    """A train 68 minutes late must still be identified when it passes.

    Ranking by scheduled time would pick whichever train was booked to leave
    latest, not the one that physically just cleared the cabin.
    """

    async def boards(settings, code):
        return {
            "TDL": {"trains": [
                # booked to leave much later, but already gone -> the real answer
                {"train": {"number": "11111", "name": "Very Late"},
                 "stop": {"departure": "18:00", "departureDay": 1},
                 "live": {"expectedDepartureTime": _iso_minutes_ago(5),
                          "delayMinutes": 68}},
                # on time but left earlier -> must NOT win
                {"train": {"number": "22222", "name": "On Time"},
                 "stop": {"departure": "17:30", "departureDay": 1},
                 "live": {"expectedDepartureTime": _iso_minutes_ago(25),
                          "delayMinutes": 0}},
            ]},
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    got = await resolve_train(make_settings(), "WSC", boards,
                              route_numbers={"11111", "22222"})
    assert got["number"] == "11111"
    assert got["sourceTime"] is not None


async def test_resolver_ignores_trains_scheduled_far_in_the_future():
    """The 10:55 question: a train hours away cannot be the one that just passed."""

    async def boards(settings, code):
        return {
            "TDL": {"trains": [
                {"train": {"number": "FUTURE", "name": "Not For Hours"},
                 "stop": {"departure": _hhmm_from_now(300), "departureDay": 1}},
            ]},
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    got = await resolve_train(make_settings(), "WSC", boards,
                              route_numbers={"FUTURE"})
    assert got is None


async def test_resolver_ignores_future_trains_but_accepts_a_departed_one():
    async def boards(settings, code):
        return {
            "TDL": {"trains": [
                {"train": {"number": "FUTURE", "name": "Not For Hours"},
                 "stop": {"departure": _hhmm_from_now(300), "departureDay": 1}},
                {"train": {"number": "JUSTLEFT", "name": "Just Left"},
                 "stop": {"departure": _hhmm_from_now(-4), "departureDay": 1}},
            ]},
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    got = await resolve_train(make_settings(), "WSC", boards,
                              route_numbers={"FUTURE", "JUSTLEFT"})
    assert got["number"] == "JUSTLEFT"
    async def boards(settings, code):
        return {
            "TDL": {"trains": [{"train": {"number": "15635"},
                                "stop": {"departure": "14:20", "departureDay": 1}}]},
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    got = await resolve_train(make_settings(), "WSC", boards, route_numbers={"33333"})
    assert got is None


def test_event_only_identifies_route_trains(mvis_client):
    """End-to-end: the board fallback must not pick an off-route train."""
    async def boards(settings, code):
        return {
            "TDL": {"trains": [
                {"train": {"number": "99999", "name": "Off Route"},
                 "stop": {"departure": _hhmm_from_now(-2), "departureDay": 1}},
                {"train": {"number": "30001", "name": "On Route"},
                 "stop": {"departure": _hhmm_from_now(-5), "departureDay": 1}},
            ]},
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    import app.main as m
    original = m.fetch_station_live_for
    m.fetch_station_live_for = boards
    try:
        tracks = m._tracks()
        tracks.set_schedule([{"number": "30001", "name": "On Route", "departure": "22:00"}])
        body = TestClient(m.app).post("/api/mvis/event", json={"axles": 72, "point": "WSC"}).json()
        assert body["train"]["number"] == "30001"
    finally:
        m.fetch_station_live_for = original


def test_entry_time_orders_midnight_crossing_correctly():
    """23:57 today must sort before 00:03 tomorrow.

    Tested at the time-parsing level: through the resolver this ordering also
    depends on what "now" is, which makes a wall-clock test flaky.
    """
    today_2357 = {"stop": {"departure": "23:57", "departureDay": 1}}
    tomorrow_0003 = {"stop": {"departure": "00:03", "departureDay": 2}}
    assert _entry_time(today_2357) < _entry_time(tomorrow_0003)


def test_entry_time_orders_same_day_by_clock():
    earlier = {"stop": {"departure": "10:00", "departureDay": 1}}
    later = {"stop": {"departure": "18:30", "departureDay": 1}}
    assert _entry_time(earlier) < _entry_time(later)


def test_entry_time_prefers_actual_over_scheduled():
    """A delayed train must be ranked by when it really left."""
    entry = {
        "stop": {"departure": "18:00", "departureDay": 1},
        "live": {"expectedDepartureTime": _iso_minutes_ago(30)},
    }
    assert _entry_time(entry) == int(
        (datetime.now(IST) - timedelta(minutes=30)).timestamp() // 60
    )


def test_entry_time_falls_back_to_scheduled_without_expectation():
    entry = {"stop": {"departure": "18:00", "departureDay": 1}, "live": {}}
    midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    expected = int((midnight + timedelta(hours=18)).timestamp() // 60)
    assert _entry_time(entry) == expected


async def test_resolver_orders_a_midnight_crossing_pair():
    """A train hours in the future must never be identified as the one that passed."""

    async def boards(settings, code):
        return {
            "TDL": {
                "trains": [
                    {"train": {"number": "LATE1"}, "stop": {"departure": "23:57", "departureDay": 1}},
                    {"train": {"number": "EARLY2"}, "stop": {"departure": "01:30", "departureDay": 2}},
                ]
            },
            "WSC": {"trains": []},
            "MTI": {"trains": []},
        }[code]

    # Both are in the future, so the resolver must decline to guess.
    assert await resolve_train(make_settings(), "WSC", boards) is None


# --- tracklist -------------------------------------------------------------

def test_tracklist_keeps_newest_and_caps_size():
    tl = TrackList(make_settings(mvis_track_size=3))
    for n in ["1", "2", "3", "4", "5"]:
        tl.add(TrackEntry(number=n))
    numbers = [e.number for e in tl.list()]
    assert numbers == ["5", "4", "3"]


def test_tracklist_readd_updates_in_place():
    tl = TrackList(make_settings())
    tl.add(TrackEntry(number="12505", axles=70, sensor_point="WSC"))
    tl.add(TrackEntry(number="12505", axles=80, sensor_point="MTI"))
    assert len(tl.list()) == 1
    entry = tl.get("12505")
    assert entry.axles == 80
    assert entry.sensor_point == "MTI"


def test_tracklist_prunes_stale_entries():
    # TTL of 0 means anything not refreshed in the same instant ages out.
    tl = TrackList(make_settings(mvis_track_ttl_seconds=-1))
    tl.add(TrackEntry(number="99999"))
    tl.prune()
    assert tl.list() == []


# --- endpoints -------------------------------------------------------------

@pytest.fixture
def mvis_client(monkeypatch):
    main.reset_timetable_cache()
    main._tracklist = None

    async def fake_board(settings, code):
        return await _boards(settings, code)

    async def fake_train_live(settings, number, date=None):
        return {
            "status": "running",
            "delayMinutes": 7,
            "currentLocation": {"status": "running", "lastStation": {"name": "Tundla"},
                                "nextStation": {"name": "Raja Ki Mandori"}, "speedKmh": 62},
            "lastUpdate": "2026-10-02T12:00:00Z",
        }

    monkeypatch.setattr(main, "fetch_station_live_for", fake_board)
    monkeypatch.setattr(main, "fetch_station_timetable_for", fake_board)
    monkeypatch.setattr(main, "fetch_train_live", fake_train_live)
    monkeypatch.setattr(main, "get_settings", lambda: make_settings())
    tracks = main._tracks()

    # The board's fake trains (22222/33333/...) stand in for route trains.
    tracks.set_schedule(
        [{"number": n, "name": f"Route {n}", "departure": "20:00"} for n in sorted(ROUTE_NUMBERS)]
        + [
            {"number": "30001", "name": "Queued One", "departure": "20:10"},
            {"number": "30002", "name": "Queued Two", "departure": "20:20"},
            {"number": "30003", "name": "Queued Three", "departure": "20:30"},
            {"number": "30004", "name": "Queued Four", "departure": "20:40"},
        ]
    )

    yield TestClient(main.app)
    main._tracklist = None


def _detected(body):
    """Trains the wheel sensor actually identified (queue also holds scheduled ones)."""
    return [t for t in body["trains"] if t["state"] == "detected"]


def test_event_rejects_freight_without_api_call(mvis_client):
    body = mvis_client.post("/api/mvis/event", json={"axles": 40, "point": "WSC"}).json()
    assert body["success"] is True
    assert body["accepted"] is False
    assert body["reason"] == "axles-out-of-band"
    assert body["band"] == [64, 100]
    # a rejected event must not identify a train
    assert _detected(mvis_client.get("/api/mvis/tracked").json()) == []


def test_event_rejects_faulty_high_axle_count(mvis_client):
    body = mvis_client.post("/api/mvis/event", json={"axles": 130, "point": "WSC"}).json()
    assert body["accepted"] is False
    assert body["reason"] == "axles-out-of-band"


def test_event_accepts_passenger_and_tracks_it(mvis_client):
    body = mvis_client.post("/api/mvis/event", json={"axles": 72, "point": "WSC"}).json()
    assert body["accepted"] is True
    assert body["train"]["number"] == "33333"
    assert body["train"]["axles"] == 72
    assert body["train"]["identifiedBy"] == "fallback:TDL"
    assert body["status"]["delayMinutes"] == 7
    assert body["status"]["lastStation"] == "Tundla"
    assert body["train"]["polls"] == 1


def test_event_accepts_boundary_axle_counts(mvis_client):
    for axles in (64, 100):
        body = mvis_client.post("/api/mvis/event", json={"axles": axles, "point": "MTI"}).json()
        assert body["accepted"] is True, f"{axles} axles should be accepted"


def test_event_with_explicit_number_skips_resolution(mvis_client, monkeypatch):
    async def boom(settings, code):
        raise AssertionError("should not read a board when the number is known")

    monkeypatch.setattr(main, "fetch_station_live_for", boom)
    body = mvis_client.post("/api/mvis/event",
                            json={"axles": 70, "point": "WSC", "trainNumber": "12942"}).json()
    assert body["accepted"] is True
    assert body["train"]["number"] == "12942"
    assert body["train"]["identifiedBy"] == "explicit"


def test_event_requires_axles(mvis_client):
    assert mvis_client.post("/api/mvis/event", json={"point": "WSC"}).status_code == 422


def test_event_rejects_negative_axles(mvis_client):
    assert mvis_client.post("/api/mvis/event", json={"axles": -1}).status_code == 422


def test_tracked_updates_same_train_not_a_new_row(mvis_client):
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "MTI"})
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "MTI"})
    body = mvis_client.get("/api/mvis/tracked").json()
    assert len(_detected(body)) == 1
    assert _detected(body)[0]["polls"] == 1


def test_repeat_events_without_demo_flag_reuse_same_train(mvis_client):
    """A real sensor fires once per train, so the same number is fine."""
    first = mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC"}).json()
    second = mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC"}).json()
    assert first["train"]["number"] == second["train"]["number"]
    assert len(_detected(mvis_client.get("/api/mvis/tracked").json())) == 1


def test_simulate_next_skips_already_tracked(mvis_client):
    """Demo mode must yield a different train each press so the queue fills."""
    seen = []
    for _ in range(3):
        body = mvis_client.post(
            "/api/mvis/event", json={"axles": 72, "point": "WSC", "simulateNext": True}
        ).json()
        seen.append(body["train"]["number"])

    assert len(set(seen)) == 3, f"expected 3 distinct trains, got {seen}"
    assert len(_detected(mvis_client.get("/api/mvis/tracked").json())) == 3


def test_simulate_next_reports_exhausted_when_none_left(mvis_client):
    for _ in range(5):
        mvis_client.post("/api/mvis/event",
                         json={"axles": 72, "point": "WSC", "simulateNext": True})
    body = mvis_client.post(
        "/api/mvis/event", json={"axles": 72, "point": "WSC", "simulateNext": True}
    ).json()
    # Board may or may not have a 6th candidate; either way it must not duplicate.
    if body.get("accepted"):
        tracked = [t["number"] for t in mvis_client.get("/api/mvis/tracked").json()["trains"]]
        assert len(tracked) == len(set(tracked))
    else:
        assert body["reason"] == "no-train-on-board"


def test_tracked_caps_at_track_size(mvis_client):
    for n in ["10001", "10002", "10003", "10004", "10005", "10006"]:
        mvis_client.post("/api/mvis/event",
                         json={"axles": 70, "point": "WSC", "trainNumber": n})
    body = mvis_client.get("/api/mvis/tracked").json()
    assert body["count"] == 5
    assert body["trains"][0]["number"] == "10006"


def test_poll_increments_poll_counter(mvis_client):
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "MTI"})
    body = mvis_client.post("/api/mvis/poll", json={}).json()
    assert body["refreshed"] == 1
    assert body["trains"][0]["polls"] == 2


def test_poll_refreshes_only_the_focus_train(mvis_client):
    """One call per cycle: following five trains must not cost five calls."""
    for n in ["10001", "10002", "10003", "10004", "10005"]:
        mvis_client.post("/api/mvis/event",
                         json={"axles": 70, "point": "WSC", "trainNumber": n})
    assert mvis_client.get("/api/mvis/tracked").json()["count"] == 5

    before = mvis_client.get("/api/mvis/tracked").json()
    body = mvis_client.post("/api/mvis/poll", json={}).json()

    assert body["refreshed"] == 1
    polls = {t["number"]: t["polls"] for t in body["trains"]}
    # only the newest detection (the focus) advanced
    assert polls["10005"] == 2
    assert all(polls[n] == 1 for n in ["10001", "10002", "10003", "10004"])
    assert before["focus"] == "10005"


def test_poll_all_refreshes_every_train(mvis_client):
    for n in ["10001", "10002", "10003"]:
        mvis_client.post("/api/mvis/event",
                         json={"axles": 70, "point": "WSC", "trainNumber": n})
    body = mvis_client.post("/api/mvis/poll?refresh_all=true").json()
    # the queue holds detected trains plus timetable placeholders
    assert body["refreshed"] == body["count"] == 5
    detected = [t for t in body["trains"] if t["state"] == "detected"]
    scheduled = [t for t in body["trains"] if t["state"] == "scheduled"]
    assert len(detected) == 3 and len(scheduled) == 2
    assert all(t["polls"] == 2 for t in detected)


def test_new_detection_takes_over_focus(mvis_client):
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC", "trainNumber": "10001"})
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC", "trainNumber": "10002"})
    body = mvis_client.get("/api/mvis/tracked").json()
    assert body["focus"] == "10002"
    assert body["stats"]["focusSwitches"] == 1


def test_focus_endpoint_switches_and_refreshes(mvis_client):
    for n in ["10001", "10002"]:
        mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC", "trainNumber": n})
    body = mvis_client.post("/api/mvis/focus?number=10001").json()
    assert body["focus"] == "10001"
    entry = next(t for t in body["trains"] if t["number"] == "10001")
    assert entry["polls"] == 2          # brought up to date on switch


def test_focus_endpoint_404s_for_untracked(mvis_client):
    assert mvis_client.post("/api/mvis/focus?number=99999").status_code == 404


def test_tracked_reports_focus_and_staleness(mvis_client):
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC", "trainNumber": "10001"})
    body = mvis_client.get("/api/mvis/tracked").json()
    assert body["focus"] == "10001"
    assert body["trains"][0]["staleSeconds"] >= 0


def test_trim_keeps_a_valid_focus(mvis_client):
    """Overflowing the queue must not leave the poller pointing at a dropped train."""
    for n in ["10001", "10002", "10003", "10004", "10005", "10006"]:
        mvis_client.post("/api/mvis/event",
                         json={"axles": 70, "point": "WSC", "trainNumber": n})
    body = mvis_client.get("/api/mvis/tracked").json()
    assert body["count"] == 5
    assert body["focus"] in [t["number"] for t in body["trains"]]


# --- queue pre-fill and rotation ------------------------------------------

def test_queue_backfills_from_schedule(mvis_client):
    """Behind the live train the board shows what is coming, from the timetable."""
    mvis_client.post("/api/mvis/event",
                     json={"axles": 72, "point": "WSC", "trainNumber": "10001"})

    tracked = mvis_client.get("/api/mvis/tracked").json()
    assert tracked["count"] == 5, "queue should be topped up from the timetable"
    states = [t["state"] for t in tracked["trains"]]
    assert states[0] == "detected"
    assert states.count("scheduled") == 4
    for t in tracked["trains"][1:]:
        assert t["identifiedBy"] == "schedule"
        assert t["scheduledTime"]


def test_backfilled_entries_have_no_live_status(mvis_client):
    """Queued-ahead trains must not claim a live status they were never given."""
    mvis_client.post("/api/mvis/event",
                     json={"axles": 72, "point": "WSC", "trainNumber": "10001"})
    tracked = mvis_client.get("/api/mvis/tracked").json()["trains"]
    for t in tracked[1:]:
        assert t["state"] == "scheduled"
        assert t.get("status") in (None, "not-started")


def test_rotate_out_promotes_next_and_backfills(mvis_client):
    mvis_client.post("/api/mvis/event",
                     json={"axles": 72, "point": "WSC", "trainNumber": "10001"})
    assert mvis_client.get("/api/mvis/tracked").json()["count"] == 5

    body = mvis_client.post("/api/mvis/rotate?number=10001").json()
    assert body["removed"] == "10001"
    assert body["focus"] != "10001"
    assert body["count"] == 5                      # topped back up from the timetable
    assert "10001" not in [t["number"] for t in body["trains"]]


def test_rotate_out_404s_for_untracked(mvis_client):
    mvis_client.post("/api/mvis/event",
                     json={"axles": 72, "point": "WSC", "trainNumber": "10001"})
    assert mvis_client.post("/api/mvis/rotate?number=99999").status_code == 404


def test_scheduled_slots_are_not_pruned_by_ttl(mvis_client):
    """Queued-ahead trains have never been refreshed, so TTL must not drop them."""
    mvis_client.post("/api/mvis/event",
                     json={"axles": 72, "point": "WSC", "trainNumber": "10001"})
    body = mvis_client.get("/api/mvis/tracked?refresh=false").json()
    assert sum(1 for t in body["trains"] if t["state"] == "scheduled") == 4


def test_exposes_wall_clock_epoch_for_the_ui_timer(mvis_client):
    mvis_client.post("/api/mvis/event",
                     json={"axles": 72, "point": "WSC", "trainNumber": "10001"})
    t = mvis_client.get("/api/mvis/tracked").json()["trains"][0]
    assert t["refreshedAtEpoch"] > 1_600_000_000   # plausible unix seconds
    assert t["staleSeconds"] >= 0


# --- "what is coming next" must ignore trains that already left ------------

def test_schedule_is_sorted_by_departure():
    tl = TrackList(make_settings())
    tl.set_schedule([
        {"number": "30002", "departure": "20:10"},
        {"number": "30001", "departure": "20:00"},
        {"number": "30003", "departure": "20:20"},
    ])
    assert [r["number"] for r in tl.upcoming_schedule()] == ["30001", "30002", "30003"]


def test_upcoming_schedule_excludes_earlier_trains():
    tl = TrackList(make_settings())
    tl.set_schedule([
        {"number": "OLD01", "departure": "00:07"},
        {"number": "OLD02", "departure": "00:22"},
        {"number": "NEXT", "departure": "23:00"},
    ])
    rows = tl.upcoming_schedule(after="10:27")
    assert [r["number"] for r in rows] == ["NEXT"]


def test_backfill_never_queues_trains_that_already_left():
    """The bug this guards: the board filled with 00:07 trains at 10:27."""
    tl = TrackList(make_settings(mvis_track_size=4))
    tl.set_schedule([
        {"number": "OLD01", "departure": "00:07", "name": "Lichchavi"},
        {"number": "OLD02", "departure": "00:22", "name": "Gaya"},
        {"number": "NEXT", "departure": "23:00", "name": "Tonight"},
        {"number": "LATER", "departure": "23:30", "name": "Tomorrow-ish"},
    ])
    tl.add(TrackEntry(number="10001", axles=72, sensor_point="WSC"))
    numbers = [e.number for e in tl.list()]
    assert "10001" in numbers
    assert "NEXT" in numbers
    assert not any(n.startswith("OLD") for n in numbers), numbers


def test_unparseable_departure_sorts_last():
    tl = TrackList(make_settings())
    tl.set_schedule([
        {"number": "BAD", "departure": "n/a"},
        {"number": "GOOD", "departure": "05:00"},
    ])
    assert [r["number"] for r in tl.upcoming_schedule()] == ["GOOD", "BAD"]


# --- Mitawali passage -----------------------------------------------------

def _route_payload(sequence, mti_index=10, status="running", last_updated="2026-10-03T05:00:00+05:30"):
    route = [{"sequence": i, "stationCode": f"S{i}", "stationName": f"Stop {i}",
              "isHalt": True, "status": "departed" if i < sequence else "upcoming"}
             for i in range(mti_index + 3)]
    route[mti_index]["stationCode"] = "MTI"
    route[mti_index]["stationName"] = "Mitawali"
    return {
        "status": status,
        "delayMinutes": 4,
        "lastUpdatedAt": last_updated,
        "currentLocation": {"status": status, "sequence": sequence,
                            "stationCode": route[min(sequence, len(route) - 1)]["stationCode"],
                            "stationName": "Somewhere", "speedKmh": 55},
        "route": route,
    }


def test_mti_passage_detects_train_before_mitawali():
    got = mti_passage(_route_payload(sequence=4, mti_index=10))
    assert got["present"] is True
    assert got["passed"] is False
    assert got["progress"] == 40.0


def test_mti_passage_detects_train_at_mitawali():
    got = mti_passage(_route_payload(sequence=10, mti_index=10))
    assert got["passed"] is False          # exactly at MTI: not yet clear
    assert got["progress"] == 100.0


def test_mti_passage_detects_train_beyond_mitawali():
    got = mti_passage(_route_payload(sequence=12, mti_index=10))
    assert got["passed"] is True


def test_mti_passage_handles_missing_destination():
    payload = _route_payload(sequence=4, mti_index=10)
    payload["route"] = [s for s in payload["route"] if s["stationCode"] != "MTI"]
    got = mti_passage(payload)
    assert got["present"] is False
    assert got["passed"] is False


def test_mti_passage_handles_missing_sequence():
    payload = _route_payload(sequence=4)
    payload["currentLocation"].pop("sequence")
    got = mti_passage(payload)
    assert got["passed"] is False
    assert got["progress"] is None


async def test_refresh_exposes_passage_and_uses_upstream_timestamp(monkeypatch):
    """The UI timer must count from RailRadar's own stamp, not our fetch time."""
    payload = _route_payload(sequence=12, mti_index=10,
                             last_updated="2026-10-03T05:00:00+05:30")

    async def fake_live(settings, number, date=None):
        return payload

    main._tracklist = None
    monkeypatch.setattr(main, "get_settings", lambda: make_settings())
    monkeypatch.setattr(main, "fetch_train_live", fake_live)

    try:
        entry = TrackEntry(number="12345")
        status = await main._refresh_entry(make_settings(), entry)
        assert status["passedMti"] is True
        assert status["mtiPresent"] is True
        assert status["mtiIndex"] == 10
        assert status["trainSequence"] == 12
        assert status["segmentProgressPct"] == 100.0
        # the timer counts from RailRadar's stamp (2026-10-03T05:00 IST), not now
        assert entry.refreshed_wall == pytest.approx(1790983800.0, abs=1)
    finally:
        main._tracklist = None


async def test_poller_rotates_a_train_that_passed_mitawali(monkeypatch):
    """Once the live train is past MTI it leaves the queue and the next is promoted."""
    main._tracklist = None
    tracks = main._tracks()
    tracks.set_schedule([
        {"number": "30001", "name": "Next Up", "departure": "20:00"},
    ])

    calls = []

    async def fake_live(settings, number, date=None):
        calls.append(number)
        if number == "10001":
            return _route_payload(sequence=12, mti_index=10)     # past MTI
        return _route_payload(sequence=3, mti_index=10)          # still coming

    monkeypatch.setattr(main, "get_settings", lambda: make_settings())
    monkeypatch.setattr(main, "fetch_train_live", fake_live)

    try:
        tracks.add(TrackEntry(number="10001", axles=72, sensor_point="WSC"))
        assert tracks.focus().number == "10001"

        # one cycle of the loop body
        entry = tracks.focus()
        await main._refresh_entry(make_settings(), entry)
        assert entry.status["passedMti"] is True

        tracks.rotate_out(entry.number)
        nxt = tracks.focus()
        await main._refresh_entry(make_settings(), nxt)

        numbers = [t.number for t in tracks.list()]
        assert "10001" not in numbers
        assert nxt.number in numbers
        assert nxt.status["passedMti"] is False
        assert calls == ["10001", nxt.number]
    finally:
        main._tracklist = None


def test_config_endpoint_reports_band(mvis_client):
    body = mvis_client.get("/api/mvis/config").json()
    assert body["axleMin"] == 64
    assert body["axleMax"] == 100
    assert body["sensorPoints"] == ["WSC", "MTI"]
    assert body["pollSeconds"] == 10


def test_stats_track_accepted_and_rejected(mvis_client):
    mvis_client.post("/api/mvis/event", json={"axles": 40, "point": "WSC"})
    mvis_client.post("/api/mvis/event", json={"axles": 70, "point": "WSC"})
    stats = mvis_client.get("/api/mvis/config").json()["stats"]
    assert stats["events"] == 2
    assert stats["accepted"] == 1
    assert stats["rejectedAxles"] == 1