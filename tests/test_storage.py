from datetime import UTC, datetime
from pathlib import Path

from monitor.models import ServiceState, Status
from monitor.storage import StateStore

URL = "https://site.test/health"


async def test_unknown_service_starts_fresh(store: StateStore) -> None:
    state = await store.load("Site", URL)
    assert state == ServiceState(name="Site", url=URL)
    assert state.status is Status.UNKNOWN


async def test_roundtrip_all_fields(db_path: Path) -> None:
    now = datetime(2026, 9, 17, 8, 20, 30, 123456, tzinfo=UTC)
    original = ServiceState(
        name="Site",
        url=URL,
        status=Status.DOWN,
        status_since=now,
        failure_count=3,
        recovery_count=0,
        failing_since=now,
        down_since=now,
        last_check=now,
        last_success=None,
        last_failure=now,
        last_error="HTTP 502",
        last_status_code=502,
        response_time=1.42,
        notified_status=Status.DOWN,
    )
    async with StateStore(db_path) as store:
        await store.save(original)
        original.failure_count = 4  # upsert overwrites
        await store.save(original)

    async with StateStore(db_path) as store:
        loaded = await store.load("Site", URL)
    assert loaded == original
    assert loaded.status is Status.DOWN
    assert loaded.last_check is not None and loaded.last_check.tzinfo is not None


async def test_url_change_resets_state(store: StateStore) -> None:
    await store.save(ServiceState(name="Site", url=URL, status=Status.DOWN, failure_count=5))
    state = await store.load("Site", "https://new.test/health")
    assert state.status is Status.UNKNOWN
    assert state.failure_count == 0


async def test_services_are_independent(store: StateStore) -> None:
    await store.save(ServiceState(name="A", url=URL, status=Status.DOWN))
    await store.save(ServiceState(name="B", url=URL, status=Status.UP))
    assert (await store.load("A", URL)).status is Status.DOWN
    assert (await store.load("B", URL)).status is Status.UP


async def test_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "db.sqlite"
    async with StateStore(path):
        pass
    assert path.exists()


async def test_managed_services_crud(store: StateStore) -> None:
    assert await store.list_services() == []
    await store.add_service("A", URL)
    await store.add_service("B", "https://b.test")
    assert [(r.name, r.url, r.enabled) for r in await store.list_services()] == [
        ("A", URL, True),
        ("B", "https://b.test", True),
    ]

    await store.set_service_enabled("B", False)
    assert [r.enabled for r in await store.list_services()] == [True, False]

    await store.save(ServiceState(name="A", url=URL, status=Status.DOWN))
    await store.delete_service("A")
    assert [r.name for r in await store.list_services()] == ["B"]
    assert (await store.load("A", URL)).status is Status.UNKNOWN  # state is gone too
