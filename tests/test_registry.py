"""The service list: config sites are read-only, Telegram sites live in SQLite."""

from pathlib import Path

import pytest

from monitor.config import ServiceConfig
from monitor.registry import RegistryError, ServiceRegistry, ServiceSource, service_id
from monitor.storage import StateStore

CONFIG = [
    ServiceConfig(name="From config", url="https://cfg.test", health_url="https://cfg.test/h")
]


async def make_registry(
    store: StateStore, config: list[ServiceConfig] | None = None
) -> ServiceRegistry:
    registry = ServiceRegistry(store, CONFIG if config is None else config)
    await registry.load()
    return registry


async def test_add_uses_domain_as_name(store: StateStore) -> None:
    registry = await make_registry(store)
    service = await registry.add("https://www.shop.test/health")
    assert service.name == "shop.test"
    assert service.url == "https://www.shop.test/health"
    assert service.source is ServiceSource.TELEGRAM
    assert service.editable is True
    assert [s.name for s in registry.all()] == ["From config", "shop.test"]


async def test_add_accepts_name_and_bare_domain(store: StateStore) -> None:
    registry = await make_registry(store)
    service = await registry.add("shop.test", "Мой магазин")
    assert service.url == "https://shop.test"
    assert service.name == "Мой магазин"


async def test_added_services_survive_restart(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        registry = await make_registry(store)
        await registry.add("https://a.test", "A")
        await registry.add("https://b.test", "B")
        await registry.set_enabled("B", False)

    async with StateStore(db_path) as store:
        registry = await make_registry(store)
        assert [s.name for s in registry.all()] == ["From config", "A", "B"]
        assert [s.name for s in registry.enabled()] == ["From config", "A"]
        assert registry.get("B") is not None and registry.get("B").enabled is False


async def test_duplicate_url_and_name_are_rejected(store: StateStore) -> None:
    registry = await make_registry(store)
    await registry.add("https://a.test", "A")
    with pytest.raises(RegistryError, match="уже есть в списке"):
        await registry.add("https://a.test")
    with pytest.raises(RegistryError, match="уже занято"):
        await registry.add("https://other.test", "A")
    with pytest.raises(RegistryError, match="уже занято"):
        await registry.add("https://other.test", "From config")


async def test_duplicate_domain_gets_unique_name(store: StateStore) -> None:
    registry = await make_registry(store)
    first = await registry.add("https://a.test/one")
    second = await registry.add("https://a.test/two")
    assert (first.name, second.name) == ("a.test", "a.test (2)")


async def test_invalid_url_is_rejected(store: StateStore) -> None:
    registry = await make_registry(store)
    with pytest.raises(RegistryError, match="Не похоже на ссылку"):
        await registry.add("ftp://a.test")


async def test_config_services_cannot_be_changed(store: StateStore) -> None:
    registry = await make_registry(store)
    with pytest.raises(RegistryError, match=r"config\.yaml"):
        await registry.remove("From config")
    with pytest.raises(RegistryError, match=r"config\.yaml"):
        await registry.set_enabled("From config", False)
    service = registry.get("From config")
    assert service is not None
    assert service.editable is False
    assert service.check_url == "https://cfg.test/h"


async def test_remove_deletes_service_and_state(store: StateStore) -> None:
    registry = await make_registry(store)
    service = await registry.add("https://a.test", "A")
    await store.save(await store.load("A", service.url))
    await registry.remove("A")

    assert registry.get("A") is None
    assert await store.list_services() == []
    fresh = await store.load("A", service.url)
    assert fresh.last_check is None
    with pytest.raises(RegistryError, match="не найден"):
        await registry.remove("A")


async def test_config_service_shadows_stored_one(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        registry = await make_registry(store, [])
        await registry.add("https://a.test", "Same name")

    async with StateStore(db_path) as store:
        registry = await make_registry(
            store, [ServiceConfig(name="Same name", url="https://c.test")]
        )
        assert [s.source for s in registry.all()] == [ServiceSource.CONFIG]


async def test_lookup_by_callback_id(store: StateStore) -> None:
    registry = await make_registry(store)
    service = await registry.add("https://a.test", "A")
    assert registry.by_id(service.id) is service
    assert registry.by_id(service_id("A")) is service
    assert registry.by_id("deadbeef") is None
    assert len(service.id) <= 64
