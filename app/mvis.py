"""MVIS integration: wheel-sensor driven train identification and tracking.

The MVIS wheel sensor fires when a train's axle count completes at Tundla. We
use it as the trigger for a RailRadar lookup, so we only spend API quota on
trains that actually passed rather than polling a board continuously.

Two rules decide whether an event is worth a lookup:

1. **Axle count** - only passenger trains (64-100 axles) are tracked. Freight
   and duplicate/faulty counts are dropped.
2. **Train number** - resolved from the live board at the sensor point. A cabin
   such as WSC has no RailRadar schedule, so the resolver falls back to the
   Tundla board and picks the train that most recently departed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings

# Trains run on Indian Standard Time, which has no daylight saving.
IST = timezone(timedelta(hours=5, minutes=30))


def _now_hhmm() -> str:
    """Current time in IST as HH:MM - the cut-off for 'what is coming next'."""
    return datetime.now(IST).strftime("%H:%M")


def _now_minutes() -> int:
    """Current time in IST as minutes from midnight, day-aware safe."""
    return int(datetime.now(IST).timestamp() // 60)


# How far past "now" a train's departure may be and still be considered as the
# one that just cleared the cabin. RailRadar's live window is 2h back / 4h
# ahead, so an hour of slack absorbs normal reporting lag without letting a
# train that is hours away be mistaken for the one that just passed.
_DEPARTURE_LOOKAHEAD_MINUTES = 60


def _hhmm(value: str) -> int:
    """Minutes from midnight for an "HH:MM" string; unparseable sorts last."""
    try:
        hours, minutes = str(value).split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return 10 ** 6


def mti_passage(data: dict[str, Any], dest_code: str = "MTI") -> dict[str, Any]:
    """How far the train has got towards the destination stop.

    The live payload carries the full `route` with a `sequence` per stop, plus
    `currentLocation.sequence`. So the train's position along the Tundla ->
    Mitawali segment can be read directly, and "has it cleared Mitawali yet?"
    is simply `current sequence > destination index`.

    Returns dicts with:
      present  - the destination appears in this train's route
      index    - its position in the route
      sequence - the train's current position
      passed   - the destination is behind the train (it has cleared the segment)
      progress - percent of the way from origin to destination (0-100)
    """
    route = data.get("route") or []
    cur = data.get("currentLocation") or {}
    sequence = cur.get("sequence")
    dest = dest_code.strip().upper()

    index = next((i for i, s in enumerate(route)
                  if (s.get("stationCode") or "").upper() == dest), None)

    if index is None or sequence is None:
        return {"present": False, "index": None, "sequence": sequence,
                "passed": False, "progress": None}

    # Progress is measured from the train's own origin stop (index 0) to the
    # destination, so 0% = just left its origin, 100% = reached Mitawali.
    progress = max(0.0, min(100.0, round(100.0 * sequence / index, 1))) if index else 100.0
    return {
        "present": True,
        "index": index,
        "sequence": sequence,
        "passed": sequence > index,
        "progress": progress,
    }


def is_passenger(axles: int, settings: Settings) -> bool:
    """True when the axle count falls in the configured passenger band.

    The band is inclusive at both ends: 64 and 100 both qualify. Anything below
    is freight, anything above is treated as a duplicate or faulty count.
    """
    return settings.mvis_axle_min <= axles <= settings.mvis_axle_max


@dataclass
class TrackEntry:
    """One train we are following, plus how we identified it.

    Only the `focus` entry is polled every cycle; the rest keep the last
    snapshot we already paid for. That keeps the upstream cost at one call per
    cycle instead of one per tracked train.

    `state` distinguishes a train the wheel sensor actually counted
    (`detected`) from one queued ahead of it from the timetable (`scheduled`),
    so the UI can show which is which.
    """

    number: str
    name: str = ""
    axles: int | None = None
    sensor_point: str = ""
    identified_by: str = ""          # "board:<code>" or "fallback:<code>"
    source_time: str | None = None    # board time at the sensor point
    detected_at: float = field(default_factory=time.monotonic)
    refreshed_at: float = field(default_factory=time.monotonic)
    refreshed_wall: float = field(default_factory=time.time)
    polls: int = 0
    state: str = "detected"          # "detected" | "scheduled"
    scheduled_time: str = ""         # timetable departure from the origin
    status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "axles": self.axles,
            "sensorPoint": self.sensor_point,
            "identifiedBy": self.identified_by,
            "sourceTime": self.source_time,
            "detectedAt": self.detected_at,
            "refreshedAt": self.refreshed_at,
            "refreshedAtEpoch": self.refreshed_wall,
            "polls": self.polls,
            "state": self.state,
            "scheduledTime": self.scheduled_time,
            "ageSeconds": int(time.monotonic() - self.detected_at),
            "staleSeconds": int(time.time() - self.refreshed_wall),
            **self.status,
        }


def _entry_time(entry: dict[str, Any]) -> int | None:
    """Minutes from the epoch for a live-board entry, so times sort correctly.

    Prefers the *actual* (expected) departure/arrival over the scheduled one so
    a delayed train is ranked by when it really left, not when it was booked
    to. Without this a train running 68 minutes late is ranked by its stale
    scheduled time and the wrong train is identified as "the one that just
    departed".

    Everything is normalised to epoch minutes - mixing in `day * 1440 + HH:MM`
    would put midnight-crossing trains and absolute times on different scales.
    Falls back to the scheduled time when RailRadar has no expectation yet.
    """
    live = entry.get("live") or {}
    stop = entry.get("stop") or {}

    for key in ("expectedDepartureTime", "expectedArrivalTime"):
        value = live.get(key)
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=IST)
                return int(parsed.timestamp() // 60)
            except (TypeError, ValueError):
                pass

    for key in ("departure", "arrival"):
        value = stop.get(key)
        if not value:
            continue
        try:
            hours, minutes = str(value).split(":")[:2]
            day = int(stop.get(f"{key}Day") or 1)
            # `departureDay` 1 is the journey's first day; 2 is the next day.
            midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
            base = midnight + timedelta(days=max(day - 1, 0))
            when = base + timedelta(hours=int(hours), minutes=int(minutes))
            return int(when.timestamp() // 60)
        except (TypeError, ValueError):
            return None
    return None


def pick_recently_departed(
    board: dict[str, Any],
    settings: Settings,
    exclude: set[str] | None = None,
    route_numbers: set[str] | None = None,
) -> dict[str, Any] | None:
    """The train on a live board that most recently departed, if any.

    Used as a fallback for sensor points with no RailRadar schedule (WSC): the
    train that has just cleared the cabin is the one the wheel sensor counted.

    `exclude` holds train numbers already being tracked. Without it the same
    train is re-detected on every event, because the board keeps showing it for
    the whole live window.

    `route_numbers` restricts candidates to trains that actually run the
    configured segment, so a train that only passes through Tundla on another
    route is never mistaken for ours.

    Ranking uses each train's *actual* departure time, so a train that is
    running hours late is still identified correctly at the moment it passes.
    """
    exclude = exclude or set()
    # The sensor fires just after a train clears the cabin, so the train we want
    # has actually left by now. Anything still scheduled to depart hours from now
    # cannot be it - that would be the "10:55 train" sir asked about.
    cutoff = _now_minutes() + _DEPARTURE_LOOKAHEAD_MINUTES

    candidates = []
    for entry in board.get("trains") or []:
        train = entry.get("train") or {}
        number = train.get("number")
        minutes = _entry_time(entry)
        if not number or minutes is None or number in exclude:
            continue
        if route_numbers and number not in route_numbers:
            continue
        if minutes > cutoff:
            continue          # has not left yet - cannot be the train that just did
        candidates.append((minutes, number, train.get("name"), entry))
    if not candidates:
        return None

    # Latest *actual* departure wins; the sensor fires just after the train clears.
    minutes, number, name, entry = max(candidates, key=lambda c: c[0])
    return {"number": number, "name": name,
            "time": _fmt_minutes(minutes),
            "delayMinutes": (entry.get("live") or {}).get("delayMinutes") or 0}


def _fmt_minutes(abs_minutes: int) -> str:
    """Render epoch minutes back to HH:MM in IST, marking later days."""
    when = datetime.fromtimestamp(abs_minutes * 60, IST)
    today = datetime.now(IST).date()
    suffix = ""
    if when.date() > today:
        suffix = f"+{(when.date() - today).days}"
    return when.strftime("%H:%M") + suffix


async def resolve_train(
    settings: Settings,
    point: str,
    fetch_board,
    *,
    train_number: str | None = None,
    exclude: set[str] | None = None,
    route_numbers: set[str] | None = None,
) -> dict[str, Any] | None:
    """Work out which train the sensor detected.

    An explicit `train_number` (from OCR or the MVIS system) wins. Otherwise we
    read the live board at the sensor point; if that point is a cabin with no
    schedule we fall back to the Tundla board and take the latest departure.

    `route_numbers` restricts the fallback to trains that actually run the
    configured segment. Without it a train that merely passes through Tundla on
    a different route (e.g. Okha - Guwahati, which never reaches Mitawali) can
    be picked as "the train that just left".
    `exclude` skips trains already being tracked.
    """
    if train_number:
        return {
            "number": train_number.strip(),
            "name": "",
            "sensorPoint": point,
            "identifiedBy": "explicit",
            "sourceTime": None,
        }

    code = point.strip().upper()
    board = await fetch_board(settings, code)
    trains = board.get("trains") or []
    if trains:
        pick = pick_recently_departed(board, settings, exclude, route_numbers)
        if pick:
            return {
                "number": pick["number"],
                "name": pick["name"] or "",
                "sensorPoint": point,
                "identifiedBy": f"board:{code}",
                "sourceTime": pick["time"],
            }

    # No data at the cabin (WSC case): the train that just left Tundla is it.
    fallback_code = settings.station
    board = await fetch_board(settings, fallback_code)
    pick = pick_recently_departed(board, settings, exclude, route_numbers)
    if not pick:
        return None
    return {
        "number": pick["number"],
        "name": pick["name"] or "",
        "sensorPoint": point,
        "identifiedBy": f"fallback:{fallback_code}",
        "sourceTime": pick["time"],
    }


class TrackList:
    """The small set of trains currently being followed (newest first).

    Exactly one entry is the `focus`: the train that just cleared Tundla. Only
    that entry is refreshed on each poll cycle. The others hold their last
    known status, so following five trains costs the same per cycle as
    following one.

    Slots behind the focus are pre-filled from the timetable (`scheduled`) so
    the board shows what is coming next. When a train is no longer on the live
    window it has left the section, so it is rotated out and the queue is topped
    up from the schedule again.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._entries: dict[str, TrackEntry] = {}
        self._order: list[str] = []
        self._focus: str | None = None
        self._schedule: list[dict[str, Any]] = []
        self.stats = {"events": 0, "accepted": 0, "rejectedAxles": 0,
                      "unresolved": 0, "apiCalls": 0, "refreshErrors": 0,
                      "focusSwitches": 0, "rotatedOut": 0, "backfilled": 0,
                      "lastRefreshAt": None}

    # --- schedule ---------------------------------------------------------
    def set_schedule(self, rows: list[dict[str, Any]]) -> None:
        """Cache the timetable rows used to fill empty slots.

        Rows are sorted by departure time so the queue shows the trains that are
        genuinely next rather than whatever order the API returned.
        """
        self._schedule = sorted(
            (r for r in rows if r.get("departure")),
            key=lambda r: _hhmm(r["departure"]),
        )

    def schedule_numbers(self) -> list[str]:
        return [r["number"] for r in self._schedule]

    def upcoming_schedule(self, after: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Schedule rows departing after `after` (HH:MM), earliest first.

        Without this the queue fills with trains that left hours ago, which is
        misleading on a board that is supposed to show what is coming next.
        """
        rows = self._schedule
        if after:
            rows = [r for r in rows if _hhmm(r["departure"]) > _hhmm(after)]
        return rows[:limit]

    def _backfill(self, after: str | None = None) -> None:
        """Top the queue up from the timetable so the board shows what is next."""
        if not self._schedule:
            return
        limit = self._settings.mvis_track_size
        known = set(self._order)
        for row in self.upcoming_schedule(after):
            if len(self._order) >= limit:
                break
            number = row["number"]
            if number in known:
                continue
            self._entries[number] = TrackEntry(
                number=number,
                name=row.get("name") or "",
                identified_by="schedule",
                state="scheduled",
                scheduled_time=row.get("departure") or "",
            )
            self._order.append(number)          # queue tail = furthest ahead
            self.stats["backfilled"] += 1

    # --- detection --------------------------------------------------------
    def add(self, entry: TrackEntry) -> None:
        """Add a detection. It becomes the focus, since it just cleared Tundla."""
        if entry.number in self._entries:
            existing = self._entries[entry.number]
            existing.axles = entry.axles
            existing.sensor_point = entry.sensor_point
            existing.identified_by = entry.identified_by
            existing.source_time = entry.source_time
            existing.detected_at = entry.detected_at
            existing.state = "detected"
        else:
            entry.state = "detected"
            self._entries[entry.number] = entry
            self._order.insert(0, entry.number)
            self._trim()

        if self._focus != entry.number:
            if self._focus is not None:
                self.stats["focusSwitches"] += 1
            self._focus = entry.number
        self._backfill(after=_now_hhmm())

    def rotate_out(self, number: str) -> bool:
        """Drop a train that has left the section, then top the queue back up."""
        if number not in self._entries:
            return False
        self._order.remove(number)
        self._entries.pop(number, None)
        self.stats["rotatedOut"] += 1
        if self._focus == number:
            self._focus = self._order[0] if self._order else None
        self._backfill()
        return True

    def prune_finished(self, on_depart: str | None = None) -> list[str]:
        """Remove trains that have left the live window.

        A train whose live status is `departed` (or an explicit list is given) has
        cleared the section, so it is rotated out and the next train is promoted.
        """
        gone = [n for n, e in self._entries.items()
                if e.state == "detected" and e.status.get("status") == on_depart]
        for n in gone:
            self.rotate_out(n)
        return gone

    def focus(self) -> TrackEntry | None:
        return self._entries.get(self._focus) if self._focus else None

    def set_focus(self, number: str) -> bool:
        if number not in self._entries:
            return False
        if self._focus != number:
            self.stats["focusSwitches"] += 1
        self._focus = number
        return True

    def _trim(self) -> None:
        limit = self._settings.mvis_track_size
        while len(self._order) > limit:
            dropped = self._order.pop()
            self._entries.pop(dropped, None)
            if self._focus == dropped:
                self._focus = self._order[0] if self._order else None

    def prune(self) -> None:
        """Drop trains that have not been refreshed inside the TTL."""
        ttl = self._settings.mvis_track_ttl_seconds
        now = time.monotonic()
        stale = [n for n in list(self._order)
                 if self._entries[n].state == "detected"
                 and now - self._entries[n].refreshed_at > ttl]
        for n in stale:
            self._order.remove(n)
            self._entries.pop(n, None)
            if self._focus == n:
                self._focus = None
        self._backfill(after=_now_hhmm())

    def list(self) -> list[TrackEntry]:
        return [self._entries[n] for n in self._order if n in self._entries]

    def get(self, number: str) -> TrackEntry | None:
        return self._entries.get(number)

    def clear(self) -> None:
        self._entries.clear()
        self._order.clear()
        self._focus = None