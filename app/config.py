from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Anchor .env to the project root so it loads regardless of the current
    # working directory the server is started from.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    railradar_api_key: str
    station_code: str = "TDL"
    hours_ahead: int = 4

    # Route we care about: trains leaving `route_from` toward `route_to`.
    # Default is Tundla Junction -> Mitawali (the west side).
    route_from: str = "TDL"
    route_to: str = "MTI"

    # --- MVIS (wheel-sensor driven) ---------------------------------------
    # Sensor/cabin points where a train is confirmed to have cleared Tundla.
    # WSC = West Side Cabin Tundla (a cabin: RailRadar lists 0 trains for it,
    # so the resolver falls back to the Tundla board). MTI = Mitawali South
    # Cabin, which RailRadar files under the plain station code MTI.
    mvis_sensor_points: str = "WSC,MTI"

    # A train is treated as passenger-only when its axle count falls in this
    # band. Below the minimum is goods/ freight; above the maximum is treated
    # as a duplicate or faulty count and skipped.
    mvis_axle_min: int = 64
    mvis_axle_max: int = 100

    # How often to refresh status for a train we are actively tracking.
    mvis_poll_seconds: int = 10

    # How many trains to keep on the active tracklist, and how long a train
    # stays on it after its last refresh.
    mvis_track_size: int = 5
    mvis_track_ttl_seconds: int = 900

    @property
    def station(self) -> str:
        return self.station_code.strip().upper()

    @property
    def route_origin(self) -> str:
        return self.route_from.strip().upper()

    @property
    def route_destination(self) -> str:
        return self.route_to.strip().upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()
