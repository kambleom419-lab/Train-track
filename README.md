# Tundla Junction — Live Trains

A single page with four things:

1. **Next 5 trains from now** — press **Get next 5 trains**. It takes the train at or before
   the current time plus the next four for today's weekday from the **complete** TDL list,
   and shows each one's Tundla status: `upcoming` / `at-station` / `departed`, with delay and
   platform. **Download full CSV** exports that complete list.
2. **Train number lookup** — type a train number (e.g. `12002`) and press **Get train status**
   for that train's live running status.
3. **All trains at Tundla** — press **Get live updates** for the live arrivals/departures at
   **Tundla Junction (TDL)** from the
   [RailRadar live station board](https://railradar.in/docs/station-live-board).
4. **Every train through Tundla (full day)** — press **Get full-day list** for *all* trains
   through TDL that day (halting and pass-through), with a filter box. Use this when the live
   board misses a train: the live board is only a ~6h window (`hoursBack: 2`), so anything
   that passed earlier disappears.

The small FastAPI backend exists only to keep your API key server-side (and avoid CORS).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -e ".[dev]"

copy .env.example .env            # then put your real RAILRADAR_API_KEY in .env
```

## Run

```bash
uvicorn app.main:app --reload
```

Open <http://localhost:8000>.

## What it does

- `GET /` — the page with all five lookups.
- `GET /api/route/trains` — **primary view**: trains running the `route_from -> route_to`
  segment (default **Tundla → Mitawali**, west side), including non-stopping trains, with live
  Tundla status merged in. Boards cached 6h.
- `GET /api/route/timetable.csv` — download that route's train list as CSV.
- `GET /api/next?count=5` — the train at/before now plus the next ones for today's weekday,
  picked from the complete TDL board, each matched against the live board for its Tundla
  status (`upcoming` / `at-station` / `departed`).
- `GET /api/station/trains` — full-day list of every train through TDL (halting + pass-through).
  Cached for 6 hours (serves the cached copy if upstream fails).
- `GET /api/timetable.csv` — download the complete timetable (all trains, all days) as CSV.
- `GET /api/train/{number}` — calls RailRadar `GET /trains/{number}/live` (optional `?date=YYYY-MM-DD`)
  and returns that train's live status.
- `GET /api/live` — calls RailRadar `GET /stations/TDL/live` and returns the Tundla board
  (a **~6h window**: 2h back, 4h ahead — so it does not retain history).

Change the station or time window in `.env` (`STATION_CODE`, `HOURS_AHEAD`).

### Where the train numbers come from

- **Route view (`/api/route/trains`)** joins the full-day boards of `ROUTE_FROM` (Tundla) and
  `ROUTE_TO` (Mitawali), keeps the origin -> destination direction (day-aware, so midnight
  crossings are handled), and includes trains that pass without halting. Both boards are cached
  6h.
- **`/api/next`** prefers the complete RailRadar TDL board, then the Google Sheet, then the
  bundled `app/timetable.csv` snapshot.

`app/timetable.csv` holds the **Tundla → Mitawali route list (136 trains)** with names, and can
be refreshed any time:

```bash
curl -o app/timetable.csv "http://localhost:8000/api/route/timetable.csv"
```

CSV columns: `Train No.`, `Running` (`Daily` or weekday numbers 1=Mon…7=Sun), `Departure Time`
(`HH:MM`), `Name`. Import the download into your Google Sheet to replace the partial list.

## Tests

```bash
pytest
```
