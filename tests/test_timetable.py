from __future__ import annotations

from datetime import datetime

from app.timetable import IST, parse_timetable, rows_from_station_board, upcoming

SAMPLE = """"Train No.","Running ","Departure Time","Coach Composition"
"10001","Daily","11:10",""
"10002","Daily","12:10",""
"10003","Daily","12:20",""
"10004","Daily","13:00",""
"10005","Daily","14:00",""
"10006","Daily","15:00",""
"10007","5","09:00",""
"10008","1","09:00",""
"""


def friday(hour: int, minute: int) -> datetime:
    # 2026-10-02 is a Friday -> isoweekday() == 5
    return datetime(2026, 10, 2, hour, minute, tzinfo=IST)


def test_parse_skips_header_and_bad_rows():
    rows = parse_timetable(SAMPLE)
    assert len(rows) == 8
    assert rows[0].number == "10001"
    assert rows[0].minutes == 11 * 60 + 10


def test_runs_on_filters_by_weekday():
    rows = {r.number: r for r in parse_timetable(SAMPLE)}
    assert rows["10001"].runs_on(3) is True          # Daily
    assert rows["10007"].runs_on(5) is True          # Friday
    assert rows["10007"].runs_on(1) is False         # not Monday
    assert rows["10008"].runs_on(1) is True


def test_upcoming_starts_at_or_before_now():
    rows = parse_timetable(SAMPLE)
    picks = upcoming(rows, friday(11, 51), count=5)
    assert [p.number for p in picks] == ["10001", "10002", "10003", "10004", "10005"]
    assert picks[0].departure == "11:10"


def test_upcoming_uses_given_count():
    rows = parse_timetable(SAMPLE)
    picks = upcoming(rows, friday(11, 51), count=2)
    assert [p.number for p in picks] == ["10001", "10002"]


def test_upcoming_before_first_train_starts_at_first():
    rows = parse_timetable(SAMPLE)
    picks = upcoming(rows, friday(8, 0), count=3)
    assert picks[0].departure == "09:00"


def test_upcoming_wraps_into_next_day():
    rows = parse_timetable(SAMPLE)
    # Sunday 23:50: anchor is the last train at/before now (15:00), then wraps into
    # Monday (10008 runs Monday at 09:00).
    sunday_late = datetime(2026, 10, 4, 23, 50, tzinfo=IST)
    picks = upcoming(rows, sunday_late, count=2)
    assert [p.number for p in picks] == ["10006", "10008"]


def test_bundled_snapshot_parses():
    from app.timetable import LOCAL_CSV

    rows = parse_timetable(LOCAL_CSV.read_text(encoding="utf-8"))
    assert len(rows) > 80
    assert any(r.number == "12419" for r in rows)


def test_rows_from_station_board_maps_days_times_and_names():
    board = {
        "trains": [
            {
                "train": {"number": "12942", "name": "Parasnath Express", "runDays": ["mon", "wed", "fri"]},
                "stop": {"arrival": "11:50", "departure": "11:55"},
            },
            {
                # terminating train: no departure, use arrival
                "train": {"number": "12345", "name": "Daily Terminator", "runDays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                "stop": {"arrival": "10:00", "departure": None},
            },
            {"train": {"number": "bad"}, "stop": {"departure": "nope"}},
        ]
    }
    rows = rows_from_station_board(board)
    assert [(r.number, r.running, r.departure, r.name) for r in rows] == [
        ("12942", "1,3,5", "11:55", "Parasnath Express"),
        ("12345", "Daily", "10:00", "Daily Terminator"),
    ]


def test_rows_from_station_board_dedupes_repeats():
    board = {
        "trains": [
            {"train": {"number": "12349", "name": "Humsafar", "runDays": ["mon"]}, "stop": {"departure": "09:50"}},
            {"train": {"number": "12349", "name": "Humsafar", "runDays": ["mon"]}, "stop": {"departure": "09:50"}},
        ]
    }
    assert len(rows_from_station_board(board)) == 1
