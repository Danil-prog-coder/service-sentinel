"""Configuration: non-secret settings from YAML, secrets and paths from ENV."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


class ConfigError(Exception):
    pass


def parse_duration(value: Any) -> float:
    """Parse ``"10m"``, ``"5s"``, ``"500ms"``, ``"1h"`` or a plain number of seconds."""
    if isinstance(value, bool):
        raise ValueError("duration must be a number or a string like '10m'")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            return float(match.group(1)) * _DURATION_UNITS[match.group(2)]
    raise ValueError(f"invalid duration {value!r}, expected e.g. '500ms', '5s', '10m', '1h'")


Duration = Annotated[float, BeforeValidator(parse_duration), Field(gt=0)]


def _validate_http_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError(f"invalid URL {value!r}: {exc}") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError(f"URL must be absolute http(s) URL, got {value!r}")
    return value


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    url: str
    health_url: str | None = None
    """Optional separate endpoint to check; ``url`` is still shown in alerts."""
    enabled: bool = True
    timeout: Duration | None = None
    follow_redirects: bool = True
    failure_threshold: int | None = Field(default=None, ge=1)
    recovery_threshold: int | None = Field(default=None, ge=1)

    @field_validator("url", "health_url")
    @classmethod
    def _check_urls(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)

    @property
    def check_url(self) -> str:
        return self.health_url or self.url


class MonitorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interval: Duration = 600.0
    failure_threshold: int = Field(default=2, ge=1)
    recovery_threshold: int = Field(default=1, ge=1)
    request_timeout: Duration = 5.0
    max_concurrency: int = Field(default=10, ge=1, le=200)
    timezone: str = "Europe/Moscow"
    user_agent: str = "service-sentinel/0.1"
    shutdown_timeout: Duration = 15.0

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    monitor: MonitorSettings = MonitorSettings()
    services: list[ServiceConfig] = Field(default_factory=list)

    @field_validator("services")
    @classmethod
    def _unique_names(cls, services: list[ServiceConfig]) -> list[ServiceConfig]:
        seen: set[str] = set()
        for service in services:
            if service.name in seen:
                raise ValueError(f"duplicate service name {service.name!r}")
            seen.add(service.name)
        return services

    @property
    def enabled_services(self) -> list[ServiceConfig]:
        return [s for s in self.services if s.enabled]


def load_config(path: Path) -> AppConfig:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return AppConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc


@dataclass(frozen=True, slots=True)
class EnvSettings:
    """Settings that come only from environment variables (secrets and paths)."""

    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    config_path: Path = Path("config.yaml")
    db_path: Path = Path("data/monitor.db")
    heartbeat_path: Path = Path("data/heartbeat.json")
    log_level: str = "INFO"
    log_format: str = "json"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EnvSettings":
        env = os.environ if environ is None else environ

        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        defaults = cls()
        return cls(
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT_ID"),
            config_path=Path(get("CONFIG_PATH") or defaults.config_path),
            db_path=Path(get("DB_PATH") or defaults.db_path),
            heartbeat_path=Path(get("HEARTBEAT_PATH") or defaults.heartbeat_path),
            log_level=(get("LOG_LEVEL") or defaults.log_level).upper(),
            log_format=(get("LOG_FORMAT") or defaults.log_format).lower(),
        )


def load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader for local runs. Existing variables are never overridden."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
