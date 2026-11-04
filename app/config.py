"""Service configuration. Everything from the environment, nothing from the repository."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- storage -------------------------------------------------------------
    data_dir: Path = Field(default=Path("./var"), description="Database and file volume")
    db_filename: str = "kitchen.db"

    # A local volume, never a network filesystem — a correctness condition for SQLite.
    # busy_timeout covers the rare overlap between writers.
    sqlite_busy_timeout_ms: int = 5000

    # --- time ----------------------------------------------------------------
    # Everything is stored in UTC and displayed in this zone. No naive dates exist.
    display_timezone: str = "UTC"

    # --- network -------------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 — inside a container, exposed only through Caddy
    port: int = 8080

    # The address the outside world reaches this service at — and therefore the OAuth
    # issuer, which must match byte for byte what a client discovered. Empty means the
    # service is not published: the OAuth endpoints then refuse to issue anything, so a
    # misconfigured deployment cannot hand out tokens for an address nobody can check.
    public_base_url: str | None = None

    # --- external services (filled in before deployment) ----------------------
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    telegram_bot_token: str | None = None
    telegram_owner_chat_id: int | None = None
    silpo_mcp_url: str = "https://mcp.silpo.ua/mcp"
    # Directory holding Garmin tokens. Defaults to the data volume; on a desktop it is
    # convenient to point at an existing ~/.garminconnect rather than keep a second copy.
    garmin_token_dir: Path | None = None
    # Writes the week without announcing it. Keep it off until the plans have been watched
    # for a few weeks: see what the service writes before it stops telling you.
    gym_plan_silent: bool = False
    # Test workouts (FTP ramp, threshold) kept on the calendar every six weeks.
    # `<workout id>|<label>|<weekday 0-6>;...` — empty means none. See env.example.
    garmin_threshold_tests: str = ""
    # Height for body-fat and waist-to-height. Empty means those are not computed.
    body_height_cm: float | None = None

    # --- backups --------------------------------------------------------------
    backup_dir: Path = Field(default=Path("./var/backups"))
    backup_interval_minutes: int = 15  # the RPO the owner chose
    backup_keep_intraday: int = 96  # a day of copies at 15-minute steps
    backup_keep_daily: int = 30
    backup_keep_monthly: int = 12
    # The OFF-BOX copy. Any command will do — scp, rclone, rsync; {path} is replaced
    # with the backup file. Empty means there is no copy off the box, and that is said
    # out loud every day: a silently missing second copy is worse than a missing one.
    backup_offsite_command: str | None = None

    # --- operations -------------------------------------------------------------
    # The alert channel is NOT Telegram: if the bot breaks, you must hear it elsewhere.
    alert_webhook_url: str | None = None
    alert_email: str | None = None

    log_level: str = "INFO"
    environment: str = "dev"

    @model_validator(mode="before")
    @classmethod
    def _blank_means_absent(cls, values):
        """An empty string in .env means "not set", not a value.

        The configuration template ships with `TELEGRAM_OWNER_CHAT_ID=` before the owner
        fills in any secrets. Without this the service will not start at all: an empty
        string does not parse as a number and the whole process dies.
        """
        if isinstance(values, dict):
            return {k: (None if isinstance(v, str) and v.strip() == "" else v)
                    for k, v in values.items()}
        return values

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_timezone)

    @property
    def issuer(self) -> str:
        """The OAuth issuer, without a trailing slash. Empty when nothing is published."""
        return (self.public_base_url or "").rstrip("/")

    @property
    def canonical_resource(self) -> str:
        """What a token is issued for: the MCP endpoint, spelled as the client sees it."""
        return f"{self.issuer}/mcp" if self.issuer else ""

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
