from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tz: str = Field(default="Australia/Melbourne")
    database_path: Path = Field(default=Path("/data/shifter.db"))
    screenshot_dir: Path = Field(default=Path("/data/screenshots"))
    screenshot_retention_days: int = Field(default=90, ge=1)
    api_key: str = Field(default="")
    allowed_users: str = Field(default="")
    fy_start_month: int = Field(default=7, ge=1, le=12)
    ha_debounce_minutes: int = Field(default=15, ge=0)
    # Open shifts older than this are treated as "stale" by the HA resolver:
    # incoming events stop propagating to them, so the next day's nanny can
    # be attributed cleanly. The stale shift stays open in the DB and is
    # surfaced on the dashboard for manual cleanup.
    # 16h fits a long overnight shift (e.g. 7pm Mon → 9am Tue) comfortably.
    shift_stale_hours: int = Field(default=16, ge=1)
    # Optional. When set, the dashboard renders a "review on Frigate" link next
    # to each pending HA-attributed shift, scoped to that shift's calendar day.
    # e.g. "https://frigate.example.com" — no trailing slash.
    frigate_base_url: str = Field(default="")

    # Signal-based HA architecture (ha_signals module).
    # Window (minutes) before/after scheduled shift start where an access_granted
    # is accepted as a nanny arrival.  Wide enough to absorb early/late arrivals.
    pre_shift_window_minutes: int = Field(default=90, ge=1)
    # Departure detection is suppressed for this many minutes after any
    # access_granted signal — absorbs the homeowner's own entry PIR / Frigate hit.
    entry_suppression_minutes: int = Field(default=3, ge=0)
    # Window (minutes) after an access_denied in which an entry_pir is treated
    # as a "let-in" fallback arrival (someone inside opened the door for the visitor).
    failed_entry_window_minutes: int = Field(default=5, ge=1)

    # DEV ONLY. When true, auth is bypassed: routes assume `dev_user`
    # and the API key check is skipped. NEVER set in production.
    dev_mode: bool = Field(default=False)
    dev_user: str = Field(default="dev")

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if invalid
        return v

    @model_validator(mode="after")
    def _api_key_required_outside_dev(self) -> "Settings":
        if not self.dev_mode and len(self.api_key) < 8:
            raise ValueError(
                "API_KEY must be at least 8 characters when DEV_MODE is not set."
            )
        return self

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def allowed_user_set(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.allowed_users.split(",") if u.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
