"""The list of monitored services: sites from ``config.yaml`` plus sites added from Telegram.

Config sites are read-only at runtime (the file stays their single source of truth);
Telegram sites live in SQLite and can be added, paused and deleted from the bot.
"""

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from monitor.config import ServiceConfig, name_from_url, normalize_url
from monitor.storage import StateStore

logger = logging.getLogger(__name__)

MAX_TELEGRAM_SERVICES = 100


class RegistryError(Exception):
    """A service cannot be added/changed; the message is shown to the user as is."""


class ServiceSource(StrEnum):
    CONFIG = "config"
    TELEGRAM = "telegram"


def service_id(name: str) -> str:
    """Short stable id for callback_data (Telegram limits it to 64 bytes)."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True, slots=True)
class ManagedService:
    config: ServiceConfig
    source: ServiceSource

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def url(self) -> str:
        return self.config.url

    @property
    def check_url(self) -> str:
        return self.config.check_url

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def id(self) -> str:
        return service_id(self.config.name)

    @property
    def editable(self) -> bool:
        """Whether the bot may pause or delete it (config sites are file-managed)."""
        return self.source is ServiceSource.TELEGRAM


class ServiceRegistry:
    def __init__(self, store: StateStore, config_services: Iterable[ServiceConfig] = ()) -> None:
        self._store = store
        self._config = {s.name: ManagedService(s, ServiceSource.CONFIG) for s in config_services}
        self._telegram: dict[str, ManagedService] = {}

    async def load(self) -> None:
        """(Re)read the Telegram-managed services from SQLite."""
        loaded: dict[str, ManagedService] = {}
        for record in await self._store.list_services():
            if record.name in self._config:
                logger.warning(
                    "telegram service is shadowed by a config service, ignoring",
                    extra={"service": record.name},
                )
                continue
            loaded[record.name] = ManagedService(
                ServiceConfig(name=record.name, url=record.url, enabled=record.enabled),
                ServiceSource.TELEGRAM,
            )
        self._telegram = loaded

    def all(self) -> list[ManagedService]:
        return [*self._config.values(), *self._telegram.values()]

    def enabled(self) -> list[ManagedService]:
        return [service for service in self.all() if service.enabled]

    def get(self, name: str) -> ManagedService | None:
        return self._config.get(name) or self._telegram.get(name)

    def by_id(self, sid: str) -> ManagedService | None:
        return next((service for service in self.all() if service.id == sid), None)

    async def add(self, raw_url: str, name: str | None = None) -> ManagedService:
        """Add a site from Telegram. ``name`` defaults to the domain, made unique if needed."""
        try:
            url = normalize_url(raw_url)
        except ValueError as exc:
            raise RegistryError(f"Не похоже на ссылку: {exc}") from exc
        if len(self._telegram) >= MAX_TELEGRAM_SERVICES:
            raise RegistryError(f"Слишком много сайтов (максимум {MAX_TELEGRAM_SERVICES}).")

        existing = next((s for s in self.all() if s.check_url == url), None)
        if existing is not None:
            raise RegistryError(f"Этот адрес уже есть в списке: {existing.name}")

        title = (name or "").strip() or name_from_url(url)
        if len(title) > 64:
            title = title[:64].rstrip()
        if self.get(title) is not None:
            if name:
                raise RegistryError(f"Название «{title}» уже занято.")
            title = self._unique_name(title)

        record = await self._store.add_service(title, url)
        service = ManagedService(
            ServiceConfig(name=record.name, url=record.url), ServiceSource.TELEGRAM
        )
        self._telegram[service.name] = service
        return service

    def _unique_name(self, base: str) -> str:
        for suffix in range(2, MAX_TELEGRAM_SERVICES + 2):
            candidate = f"{base} ({suffix})"
            if self.get(candidate) is None:
                return candidate
        raise RegistryError("Не удалось подобрать свободное название.")

    async def remove(self, name: str) -> ManagedService:
        service = self._require_editable(name)
        await self._store.delete_service(name)
        del self._telegram[name]
        return service

    async def set_enabled(self, name: str, enabled: bool) -> ManagedService:
        service = self._require_editable(name)
        if service.enabled is enabled:
            return service
        await self._store.set_service_enabled(name, enabled)
        updated = ManagedService(
            service.config.model_copy(update={"enabled": enabled}), service.source
        )
        self._telegram[name] = updated
        logger.info(
            "service %s from telegram", "resumed" if enabled else "paused", extra={"service": name}
        )
        return updated

    def _require_editable(self, name: str) -> ManagedService:
        service = self.get(name)
        if service is None:
            raise RegistryError("Сайт не найден, возможно он уже удалён.")
        if not service.editable:
            raise RegistryError(
                f"«{name}» описан в config.yaml — пауза и удаление только через файл."
            )
        return service
