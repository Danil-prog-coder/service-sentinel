"""SQLite persistence: service state plus the sites added from Telegram."""

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite

from monitor.models import ServiceState, Status

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_state (
    name              TEXT PRIMARY KEY,
    url               TEXT NOT NULL,
    status            TEXT NOT NULL,
    status_since      TEXT,
    failure_count     INTEGER NOT NULL DEFAULT 0,
    recovery_count    INTEGER NOT NULL DEFAULT 0,
    failing_since     TEXT,
    down_since        TEXT,
    last_check        TEXT,
    last_success      TEXT,
    last_failure      TEXT,
    last_error        TEXT,
    last_status_code  INTEGER,
    response_time     REAL,
    notified_status   TEXT,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

# Sites added from Telegram. Sites from config.yaml are NOT stored here: the file
# stays the single source of truth for them.
_SERVICES_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS managed_service (
    name       TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT ({_NOW})
)
"""


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    """One row of ``managed_service``: a site the user added from Telegram."""

    name: str
    url: str
    enabled: bool = True


_FIELDS = [f.name for f in dataclasses.fields(ServiceState)]
_DATETIME_FIELDS = {f.name for f in dataclasses.fields(ServiceState) if "datetime" in str(f.type)}
_UPSERT = (
    f"INSERT INTO service_state ({', '.join(_FIELDS)}) "  # noqa: S608 - static column names
    f"VALUES ({', '.join('?' for _ in _FIELDS)}) "
    "ON CONFLICT(name) DO UPDATE SET "
    + ", ".join(f"{f} = excluded.{f}" for f in _FIELDS if f != "name")
    + ", updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
)


def _to_db(state: ServiceState) -> list[Any]:
    values: list[Any] = []
    for name in _FIELDS:
        value = getattr(state, name)
        if isinstance(value, datetime):
            value = value.isoformat()
        values.append(value)
    return values


def _from_db(row: aiosqlite.Row) -> ServiceState:
    data: dict[str, Any] = {name: row[name] for name in _FIELDS}
    for name in _DATETIME_FIELDS:
        if data[name] is not None:
            data[name] = datetime.fromisoformat(data[name])
    data["status"] = Status(data["status"])
    if data["notified_status"] is not None:
        data["notified_status"] = Status(data["notified_status"])
    return ServiceState(**data)


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(_SCHEMA)
        await self._db.execute(_SERVICES_SCHEMA)
        await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._db.commit()
        logger.info("state storage opened", extra={"db_path": str(self._path)})

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("state storage closed")

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateStore is not open")
        return self._db

    async def load(self, name: str, url: str) -> ServiceState:
        """Load saved state; start fresh if unknown or if the checked URL changed."""
        async with self._conn.execute(
            "SELECT * FROM service_state WHERE name = ?", (name,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return ServiceState(name=name, url=url)
        state = _from_db(row)
        if state.url != url:
            logger.info(
                "service URL changed, resetting state",
                extra={"service": name, "old_url": state.url, "url": url},
            )
            return ServiceState(name=name, url=url)
        return state

    async def save(self, state: ServiceState) -> None:
        await self._conn.execute(_UPSERT, _to_db(state))
        await self._conn.commit()

    async def delete_state(self, name: str) -> None:
        await self._conn.execute("DELETE FROM service_state WHERE name = ?", (name,))
        await self._conn.commit()

    # --- sites added from Telegram -------------------------------------------------

    async def list_services(self) -> list[ServiceRecord]:
        """Telegram-managed sites, oldest first (the order shown in the bot)."""
        async with self._conn.execute(
            "SELECT name, url, enabled FROM managed_service ORDER BY created_at, name"
        ) as cursor:
            rows = await cursor.fetchall()
        return [ServiceRecord(r["name"], r["url"], bool(r["enabled"])) for r in rows]

    async def add_service(self, name: str, url: str) -> ServiceRecord:
        await self._conn.execute(
            "INSERT INTO managed_service (name, url) VALUES (?, ?)", (name, url)
        )
        await self._conn.commit()
        logger.info("service added from telegram", extra={"service": name, "url": url})
        return ServiceRecord(name, url)

    async def set_service_enabled(self, name: str, enabled: bool) -> None:
        await self._conn.execute(
            "UPDATE managed_service SET enabled = ? WHERE name = ?", (int(enabled), name)
        )
        await self._conn.commit()

    async def delete_service(self, name: str) -> None:
        await self._conn.execute("DELETE FROM managed_service WHERE name = ?", (name,))
        await self._conn.execute("DELETE FROM service_state WHERE name = ?", (name,))
        await self._conn.commit()
        logger.info("service deleted from telegram", extra={"service": name})
