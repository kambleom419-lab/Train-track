from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.railradar import RailRadarError

SAMPLE = {
    "station": {"code": "TDL", "name": "Tundla Junction"},
    "count": 1,
    "trains": [
        {
            "train": {"number": "12002", "name": "Bhopal Shatabdi", "source": "NDLS", "destination": "BPL"},
            "stop": {"arrival": "12:00", "departure": "12:02"},
            "live": {"type": "upcoming", "expectedArrivalTime": "2026-10-02T12:00:00+05:30", "delayMinutes": 8, "platform": "2"},
        }
    ],
}

SAMPLE_TRAIN = {
    "trainNumber": "12002",
    "trainName": "Bhopal Shatabdi",
    "status": "running",
    "delayMinutes": 8,
    "train": {"number": "12002", "name": "Bhopal Shatabdi", "source": {"code": "NDLS", "name": "New Delhi"}, "destination": {"code": "BPL", "name": "Bhopal"}},
    "currentLocation": {"stationCode": "AGC", "status": "departed", "speedKmh": 92.5, "segmentProgress": 0.4},
    "nextHalt": {"stationCode": "GWL", "stationName": "Gwalior", "distance": 118},
}

SAMPLE_TIMETABLE = {
    "station": {"code": "TDL", "name": "Tundla Jn"},
    "count": 1,
    "trains": [
        {
            "train": {
                "number": "12942",
                "name": "Parasnath Express",
                "source": {"code": "ASN", "name": "Asansol"},
                "destination": {"code": "BVC", "name": "Bhavnagar"},
            },
            "stop": {"arrival": "11:50", "departure": "11:55", "stopType": "halt"},
        }
    ],
}

BOARD = {
    "station": {"code": "TDL", "name": "Tundla Jn"},
    "count": 2,
    "trains": [
        {"train": {"number": "11111", "name": "Board Express", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, "stop": {"departure": "12:10"}},
        {"train": {"number": "22222", "name": "Later Express", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, "stop": {"departure": "13:10"}},
    ],
}


def _tuesday_noon():
    from datetime import datetime

    from app.timetable import IST

    return datetime(2026, 10, 6, 12, 0, tzinfo=IST)


# --- page / config -------------------------------------------------------
def test_index_served():
    resp = TestClient(main.app).get("/")
    assert resp.status_code == 200
    assert "Tundla" in resp.text


def test_missing_api_key_reports_json(monkeypatch):
    from pydantic import ValidationError

    def boom():
        raise ValidationError.from_exception_data(
            "Settings",
            [{"type": "missing", "loc": ("railradar_api_key",), "input": {}}],
        )

    monkeypatch.setattr(main, "get_settings", boom)
    resp = TestClient(main.app).get("/api/live")
    assert resp.status_code == 500
    assert "RAILRADAR_API_KEY" in resp.json()["detail"]


# --- station live board --------------------------------------------------
def test_live_returns_upstream_data(monkeypatch):
    async def fake(settings):
        return SAMPLE

    monkeypatch.setattr(main, "fetch_station_live", fake)
    body = TestClient(main.app).get("/api/live").json()
    assert body["success"] is True
    assert body["data"]["trains"][0]["train"]["number"] == "12002"


def test_live_surfaces_upstream_error_as_502(monkeypatch):
    async def fake(settings):
        raise RailRadarError("Invalid or expired RailRadar API key")

    monkeypatch.setattr(main, "fetch_station_live", fake)
    resp = TestClient(main.app).get("/api/live")
    assert resp.status_code == 502
    assert "API key" in resp.json()["detail"]


def test_network_error_is_json_502(monkeypatch):
    async def fake(settings):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(main, "fetch_station_live", fake)
    resp = TestClient(main.app).get("/api/live")
    assert resp.status_code == 502
    assert "Could not reach RailRadar" in resp.json()["detail"]


def test_unhandled_error_still_returns_json(monkeypatch):
    async def fake(settings):
        raise ValueError("boom")

    monkeypatch.setattr(main, "fetch_station_live", fake)
    resp = TestClient(main.app, raise_server_exceptions=False).get("/api/live")
    assert resp.status_code == 500
    assert resp.json()["detail"].startswith("Internal error")


def test_live_filters_to_route_trains(monkeypatch):
    async def fake_board_for(settings, code):
        return {"TDL": TDL_BOARD, "MTI": MTI_BOARD}[code]

    async def fake_live(settings):
        return {
            "station": {"code": "TDL", "name": "Tundla Jn"},
            "trains": [
                {"train": {"number": "51903"}, "stop": {}, "live": {"type": "departed"}},
                {"train": {"number": "99999"}, "stop": {}, "live": {"type": "upcoming"}},
            ],
        }

    monkeypatch.setattr(main, "fetch_station_timetable_for", fake_board_for)
    monkeypatch.setattr(main, "fetch_station_live", fake_live)

    body = TestClient(main.app).get("/api/live").json()
    assert body["routeOnly"] is True
    assert [t["train"]["number"] for t in body["data"]["trains"]] == ["51903"]
    assert body["data"]["count"] == 1

    everything = TestClient(main.app).get("/api/live?route_only=false").json()
    assert len(everything["data"]["trains"]) == 2


# --- single train lookup -------------------------------------------------
def test_train_lookup_returns_data(monkeypatch):
    captured = {}

    async def fake(settings, number, date=None):
        captured["number"] = number
        return SAMPLE_TRAIN

    monkeypatch.setattr(main, "fetch_train_live", fake)
    body = TestClient(main.app).get("/api/train/12002").json()
    assert body["success"] is True
    assert body["data"]["trainName"] == "Bhopal Shatabdi"
    assert captured["number"] == "12002"


def test_train_lookup_rejects_non_numeric(monkeypatch):
    async def fake(settings, number, date=None):
        raise AssertionError("should not be called")

    monkeypatch.setattr(main, "fetch_train_live", fake)
    resp = TestClient(main.app).get("/api/train/abc")
    assert resp.status_code == 400
    assert "digits" in resp.json()["detail"]


def test_train_lookup_upstream_error_is_502(monkeypatch):
    async def fake(settings, number, date=None):
        raise RailRadarError("Not found - check the train number or date")

    monkeypatch.setattr(main, "fetch_train_live", fake)
    resp = TestClient(main.app).get("/api/train/99999")
    assert resp.status_code == 502
    assert "Not found" in resp.json()["detail"]


# --- full-day station timetable -----------------------------------------
def test_station_trains_returns_full_day_and_caches(monkeypatch):
    calls = {"n": 0}

    async def fake(settings):
        calls["n"] += 1
        return SAMPLE_TIMETABLE

    monkeypatch.setattr(main, "fetch_station_timetable", fake)
    client = TestClient(main.app)

    first = client.get("/api/station/trains").json()
    second = client.get("/api/station/trains").json()

    assert first["cached"] is False
    assert first["data"]["trains"][0]["train"]["number"] == "12942"
    assert second["cached"] is True
    assert calls["n"] == 1


def test_station_trains_serves_stale_cache_on_error(monkeypatch):
    async def ok(settings):
        return SAMPLE_TIMETABLE

    monkeypatch.setattr(main, "fetch_station_timetable", ok)
    client = TestClient(main.app)
    client.get("/api/station/trains")

    async def boom(settings):
        raise RailRadarError("upstream down")

    monkeypatch.setattr(main, "fetch_station_timetable", boom)
    main._timetable_cache["TDL"]["expires"] = 0

    body = client.get("/api/station/trains").json()
    assert body["stale"] is True
    assert body["data"]["trains"][0]["train"]["number"] == "12942"


def test_station_trains_502_when_no_cache(monkeypatch):
    async def boom(settings):
        raise RailRadarError("upstream down")

    monkeypatch.setattr(main, "fetch_station_timetable", boom)
    resp = TestClient(main.app).get("/api/station/trains")
    assert resp.status_code == 502


# --- next N trains -------------------------------------------------------
def test_next_trains_uses_complete_board_and_merges_status(monkeypatch):
    async def fake_board(settings):
        return BOARD

    async def fake_live(settings):
        return {
            "station": {"code": "TDL", "name": "Tundla Jn"},
            "count": 1,
            "trains": [
                {
                    "train": {"number": "11111", "name": "Board Express"},
                    "stop": {},
                    "live": {
                        "type": "departed",
                        "delayMinutes": 7,
                        "platform": "2",
                        "expectedArrivalTime": "2026-10-06T12:08:00+05:30",
                        "expectedDepartureTime": "2026-10-06T12:10:00+05:30",
                    },
                }
            ],
        }

    monkeypatch.setattr(main, "fetch_station_timetable", fake_board)
    monkeypatch.setattr(main, "fetch_station_live", fake_live)
    monkeypatch.setattr(main, "now_ist", _tuesday_noon)

    body = TestClient(main.app).get("/api/next?count=2").json()
    assert body["source"] == "railradar-board"
    assert body["count"] == 2
    assert body["trains"][0]["number"] == "11111"
    assert body["trains"][0]["name"] == "Board Express"
    assert body["trains"][0]["status"] == "departed"
    assert body["trains"][0]["delayMinutes"] == 7
    assert body["trains"][0]["atTundla"] is True
    assert body["trains"][1]["number"] == "22222"
    assert body["trains"][1]["status"] is None
    assert body["trains"][1]["atTundla"] is False


def test_next_trains_falls_back_when_board_unavailable(monkeypatch):
    from app.timetable import ScheduledTrain

    async def fake_timetable():
        return [ScheduledTrain("12002", "Daily", "11:10", "Snapshot Express")], "local-snapshot"

    monkeypatch.setattr(main, "load_timetable", fake_timetable)
    monkeypatch.setattr(main, "now_ist", _tuesday_noon)

    body = TestClient(main.app).get("/api/next?count=1").json()
    assert body["source"] == "local-snapshot"
    assert body["trains"][0]["number"] == "12002"
    assert body["trains"][0]["name"] == "Snapshot Express"


def test_next_trains_rejects_bad_count():
    resp = TestClient(main.app).get("/api/next?count=0")
    assert resp.status_code == 400


def test_timetable_csv_download(monkeypatch):
    async def fake_board(settings):
        return BOARD

    monkeypatch.setattr(main, "fetch_station_timetable", fake_board)
    resp = TestClient(main.app).get("/api/timetable.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert resp.text.splitlines()[0].startswith("Train No.")
    assert "11111" in resp.text and "Board Express" in resp.text


# --- Tundla -> Mitawali route -------------------------------------------
TDL_BOARD = {
    "station": {"code": "TDL", "name": "Tundla Jn"},
    "trains": [
        {"train": {"number": "51903", "name": "Tundla - Etah Passenger", "type": "Passenger", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, "stop": {"arrival": None, "departure": "05:00"}},
        {"train": {"number": "64583", "name": "Tundla - Delhi MEMU", "type": "MEMU", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, "stop": {"arrival": None, "departure": "06:15"}},
        {"train": {"number": "51904", "name": "Etah - Tundla Passenger", "type": "Passenger", "runDays": ["mon"]}, "stop": {"arrival": "22:20", "departure": None}},
        {"train": {"number": "99999", "name": "Tundla Only Express", "type": "Express", "runDays": ["mon"]}, "stop": {"arrival": None, "departure": "07:00"}},
    ],
}

MTI_BOARD = {
    "station": {"code": "MTI", "name": "Mitawali"},
    "trains": [
        {"train": {"number": "51903", "name": "Tundla - Etah Passenger", "type": "Passenger", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, "stop": {"arrival": "05:11", "departure": "05:12"}},
        {"train": {"number": "64583", "name": "Tundla - Delhi MEMU", "type": "MEMU", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, "stop": {"arrival": "06:24", "departure": "06:25"}},
        {"train": {"number": "51904", "name": "Etah - Tundla Passenger", "type": "Passenger", "runDays": ["mon"]}, "stop": {"arrival": "22:00", "departure": "22:01"}},
    ],
}


def test_route_trains_joins_boards_and_keeps_origin_to_destination(monkeypatch):
    async def fake_board_for(settings, code):
        return {"TDL": TDL_BOARD, "MTI": MTI_BOARD}[code]

    async def fake_live(settings):
        return {
            "station": {"code": "TDL", "name": "Tundla Jn"},
            "count": 1,
            "trains": [
                {
                    "train": {"number": "64583", "name": "Tundla - Delhi MEMU"},
                    "stop": {},
                    "live": {
                        "type": "at-station",
                        "delayMinutes": 5,
                        "platform": "3",
                        "expectedArrivalTime": "2026-10-02T06:24:00+05:30",
                        "expectedDepartureTime": "2026-10-02T06:25:00+05:30",
                    },
                }
            ],
        }

    monkeypatch.setattr(main, "fetch_station_timetable_for", fake_board_for)
    monkeypatch.setattr(main, "fetch_station_live", fake_live)

    body = TestClient(main.app).get("/api/route/trains").json()
    numbers = [t["number"] for t in body["trains"]]
    assert numbers == ["51903", "64583"]      # eastbound 51904 and Tundla-only 99999 dropped
    assert body["from"]["code"] == "TDL"
    assert body["to"]["code"] == "MTI"
    assert body["trains"][0]["status"] is None
    assert body["trains"][1]["status"] == "at-station"
    assert body["trains"][1]["delayMinutes"] == 5


def test_route_trains_caches_boards(monkeypatch):
    calls = {"n": 0}

    async def fake_board_for(settings, code):
        calls["n"] += 1
        return {"TDL": TDL_BOARD, "MTI": MTI_BOARD}[code]

    monkeypatch.setattr(main, "fetch_station_timetable_for", fake_board_for)
    client = TestClient(main.app)
    first = client.get("/api/route/trains").json()
    second = client.get("/api/route/trains").json()
    assert first["count"] == 2
    assert second["count"] == 2
    assert calls["n"] == 2          # one call per board; second request fully cached


def test_route_trains_502_when_upstream_down(monkeypatch):
    async def boom(settings, code):
        raise RailRadarError("upstream down")

    monkeypatch.setattr(main, "fetch_station_timetable_for", boom)
    resp = TestClient(main.app).get("/api/route/trains")
    assert resp.status_code == 502


def test_route_timetable_csv_download(monkeypatch):
    async def fake_board_for(settings, code):
        return {"TDL": TDL_BOARD, "MTI": MTI_BOARD}[code]

    monkeypatch.setattr(main, "fetch_station_timetable_for", fake_board_for)
    resp = TestClient(main.app).get("/api/route/timetable.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    lines = resp.text.splitlines()
    assert lines[0] == "Train No.,Running,Departure Time,Name"
    assert any(line.startswith("51903,") for line in lines)
    assert not any(line.startswith("51904,") for line in lines)   # eastbound dropped


def test_join_segment_trains_handles_midnight_direction():
    origin = {
        "trains": [
            {"train": {"number": "A1"}, "stop": {"departure": "23:57", "departureDay": 1}},
            {"train": {"number": "A2"}, "stop": {"departure": "00:02", "departureDay": 2}},
        ]
    }
    destination = {
        "trains": [
            {"train": {"number": "A1"}, "stop": {"arrival": "00:03", "arrivalDay": 2}},
            {"train": {"number": "A2"}, "stop": {"arrival": "23:57", "arrivalDay": 1}},
        ]
    }
    rows = main.join_segment_trains(origin, destination)
    # A1 leaves before it arrives (westbound across midnight); A2 is the reverse.
    assert [r["number"] for r in rows] == ["A1"]
