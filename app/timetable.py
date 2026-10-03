from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1UMOKf0GXxbENpH1IOfdb7hwpJYCETCOvTd00wgXOwmU/export?format=csv&gid=0"
)
LOCAL_CSV = Path(__file__).parent / "timetable.csv"

# India Standard Time has no DST, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))

WEEKDAY_CODES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


def now_ist() -> datetime:
    return datetime.now(IST)


@dataclass(frozen=True)
class ScheduledTrain:
    number: str
    running: str        # "Daily" or e.g. "1,3,6"
    departure: str      # "HH:MM" (departure from Tundla, or arrival for terminating trains)
    name: str = ""

    @property
    def minutes(self) -> int:
        hours, minutes = self.departure.split(":")[:2]
        return int(hours) * 60 + int(minutes)

    def runs_on(self, weekday: int) -> bool:
        """weekday: 1 = Monday ... 7 = Sunday."""
        raw = (self.running or "").strip().lower()
        if raw == "daily":
            return True
        if not raw:
            return False
        days = {int(p) for p in re.split(r"[\s,]+", raw) if p.isdigit()}
        return weekday in days


def parse_timetable(text: str) -> list[ScheduledTrain]:
    rows: list[ScheduledTrain] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 3:
            continue
        number, running, departure = row[0].strip(), row[1].strip(), row[2].strip()
        if not number.isdigit() or not re.fullmatch(r"\d{1,2}:\d{2}", departure):
            continue  # skips the header row and any stray lines
        name = row[3].strip() if len(row) > 3 else ""
        rows.append(ScheduledTrain(number=number, running=running, departure=departure, name=name))
    return rows


def running_label(run_days: Any) -> str:
    """Turn an API runDays list into the sheet form: "Daily" or e.g. "1,3,6"."""
    if not run_days:
        return "Daily"
    coded = {WEEKDAY_CODES[str(d)[:3].lower()] for d in run_days if str(d)[:3].lower() in WEEKDAY_CODES}
    if len(coded) >= 7:
        return "Daily"
    if not coded:
        return "Daily"
    return ",".join(str(n) for n in sorted(coded))


def rows_from_station_board(data: dict[str, Any]) -> list[ScheduledTrain]:
    """Turn a RailRadar station timetable payload into timetable rows.

    Uses the departure time at the station, falling back to arrival for trains
    that terminate there. De-duplicates repeated entries.
    """
    rows: list[ScheduledTrain] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in data.get("trains") or []:
        train = entry.get("train") or {}
        stop = entry.get("stop") or {}
        number = str(train.get("number") or "").strip()
        if not number.isdigit():
            continue
        when = stop.get("departure") or stop.get("arrival")
        if not when or not re.fullmatch(r"\d{1,2}:\d{2}", str(when)):
            continue
        running = running_label(train.get("runDays"))
        key = (number, str(when), running)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            ScheduledTrain(
                number=number,
                running=running,
                departure=str(when),
                name=str(train.get("name") or ""),
            )
        )
    return rows


async def load_timetable() -> tuple[list[ScheduledTrain], str]:
    """Prefer the live Google Sheet; fall back to the bundled snapshot."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(SHEET_CSV_URL)
        if resp.status_code == 200 and "Train No." in resp.text:
            return parse_timetable(resp.text), "google-sheet"
    except httpx.HTTPError:
        pass
    return parse_timetable(LOCAL_CSV.read_text(encoding="utf-8")), "local-snapshot"


def upcoming(rows: list[ScheduledTrain], now: datetime, count: int = 5) -> list[ScheduledTrain]:
    """The train scheduled at/before `now` plus the following ones, for today's weekday.

    Wraps into following days when fewer than `count` remain today.
    """
    now_minutes = now.hour * 60 + now.minute
    result: list[ScheduledTrain] = []

    for offset in range(7):
        weekday = (now.isoweekday() - 1 + offset) % 7 + 1
        day_rows = sorted((r for r in rows if r.runs_on(weekday)), key=lambda r: r.minutes)

        if offset == 0:
            start = 0
            for index, row in enumerate(day_rows):
                if row.minutes <= now_minutes:
                    start = index
                else:
                    break
            day_rows = day_rows[start:]

        result.extend(day_rows)
        if len(result) >= count:
            break

    return result[:count]
