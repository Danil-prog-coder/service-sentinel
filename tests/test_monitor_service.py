"""End-to-end monitor logic with mocked sites, mocked Telegram and a real SQLite file."""

import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from conftest import ScriptedSites, TelegramRecorder
from monitor.checker import HttpChecker
from monitor.config import MonitorSettings, ServiceConfig
from monitor.models import Status
from monitor.monitoring import MonitorService
from monitor.registry import ServiceRegistry
from monitor.storage import StateStore
from monitor.telegram import TelegramClient

SITE_A = "https://a.test/health"
SITE_B = "https://b.test/health"
SETTINGS = MonitorSettings(interval=600, failure_threshold=2, recovery_threshold=1)
SERVICES = [
    ServiceConfig(name="Site A", url=SITE_A),
    ServiceConfig(name="Site B", url="https://b.test", health_url=SITE_B),
    ServiceConfig(name="Disabled", url="https://c.test/health", enabled=False),
]


@pytest.fixture
def make_monitor(
    site_client: httpx.AsyncClient, make_telegram: Callable[..., TelegramClient]
) -> Callable[[StateStore], MonitorService]:
    def factory(store: StateStore) -> MonitorService:
        return MonitorService(
            SETTINGS,
            ServiceRegistry(store, SERVICES),
            HttpChecker(site_client),
            store,
            make_telegram(max_attempts=1),
        )

    return factory


async def run_cycles(monitor: MonitorService, n: int) -> None:
    for _ in range(n):
        await monitor.run_cycle()


async def test_healthy_sites_send_nothing(
    sites: ScriptedSites, telegram_api: TelegramRecorder, store: StateStore, make_monitor
) -> None:
    sites.set(SITE_A, 200)
    sites.set(SITE_B, 200)
    monitor = make_monitor(store)
    await monitor.load_state()
    await run_cycles(monitor, 3)

    assert telegram_api.messages == []
    assert monitor.states["Site A"].status is Status.UP
    assert monitor.states["Site B"].status is Status.UP
    # A disabled service is known (so the bot can show it) but never checked.
    assert monitor.states["Disabled"].status is Status.UNKNOWN
    assert "https://c.test/health" not in sites.calls
    assert len(sites.calls) == 6


async def test_404_does_not_alert(
    sites: ScriptedSites, telegram_api: TelegramRecorder, store: StateStore, make_monitor, caplog
) -> None:
    sites.set(SITE_A, 404)
    sites.set(SITE_B, 200)
    monitor = make_monitor(store)
    await monitor.load_state()
    with caplog.at_level(logging.WARNING):
        await run_cycles(monitor, 5)

    assert telegram_api.messages == []
    assert monitor.states["Site A"].status is Status.UNKNOWN
    assert any("client error" in r.message and r.status_code == 404 for r in caplog.records)


async def test_down_alert_sent_once_then_recovery(
    sites: ScriptedSites, telegram_api: TelegramRecorder, store: StateStore, make_monitor
) -> None:
    sites.set(SITE_A, 200, 502, 502, 502, 502, 502, 200)
    sites.set(SITE_B, 200)
    monitor = make_monitor(store)
    await monitor.load_state()

    await run_cycles(monitor, 2)  # 200, 502 -> still UP
    assert telegram_api.messages == []
    await monitor.run_cycle()  # second 502 -> DOWN
    assert len(telegram_api.messages) == 1
    down = telegram_api.messages[0]
    assert down.startswith("🔴 SERVICE DOWN")
    assert "Service: Site A" in down
    assert f"URL: {SITE_A}" in down
    assert "Status: 502 Bad Gateway" in down
    assert "Error: HTTP 502" in down
    assert "Response time:" in down
    assert "Detected at:" in down and "MSK" in down

    await run_cycles(monitor, 3)  # still failing: no duplicates
    assert len(telegram_api.messages) == 1

    await monitor.run_cycle()  # 200 -> recovered
    assert len(telegram_api.messages) == 2
    recovered = telegram_api.messages[1]
    assert recovered.startswith("🟢 SERVICE RECOVERED")
    assert "Status: 200 OK" in recovered
    assert "Downtime:" in recovered
    assert "Recovered at:" in recovered

    await run_cycles(monitor, 2)
    assert len(telegram_api.messages) == 2


async def test_timeout_alert_text(
    sites: ScriptedSites, telegram_api: TelegramRecorder, store: StateStore, make_monitor
) -> None:
    sites.set(SITE_A, 200)
    sites.set(SITE_B, httpx.ConnectTimeout("timed out"))
    monitor = make_monitor(store)
    await monitor.load_state()
    await run_cycles(monitor, 2)

    assert len(telegram_api.messages) == 1
    text = telegram_api.messages[0]
    assert "Service: Site B" in text
    assert "URL: https://b.test\n" in text
    assert f"Health check: {SITE_B}" in text
    assert "Error: request timeout after 5s" in text
    assert "Status:" not in text
    assert "Response time:" not in text


async def test_telegram_failure_is_retried_next_cycle(
    sites: ScriptedSites, telegram_api: TelegramRecorder, store: StateStore, make_monitor
) -> None:
    sites.set(SITE_A, 500)
    sites.set(SITE_B, 200)
    monitor = make_monitor(store)
    await monitor.load_state()
    telegram_api.fail_next = 1

    await run_cycles(monitor, 2)  # DOWN, but Telegram returns 500
    assert telegram_api.messages == []
    assert monitor.states["Site A"].notified_status is None

    await monitor.run_cycle()  # retried
    assert len(telegram_api.messages) == 1
    await run_cycles(monitor, 2)
    assert len(telegram_api.messages) == 1


async def test_state_survives_restart_without_duplicate_alert(
    sites: ScriptedSites,
    telegram_api: TelegramRecorder,
    db_path: Path,
    make_monitor,
) -> None:
    sites.set(SITE_A, httpx.ConnectError("Connection refused"))
    sites.set(SITE_B, 200)

    async with StateStore(db_path) as store:
        monitor = make_monitor(store)
        await monitor.load_state()
        await run_cycles(monitor, 2)
    assert len(telegram_api.messages) == 1
    assert "Error: connection refused" in telegram_api.messages[0]

    # "Restart": new store and monitor instances on the same database file.
    async with StateStore(db_path) as store:
        monitor = make_monitor(store)
        await monitor.load_state()
        state = monitor.states["Site A"]
        assert state.status is Status.DOWN
        assert state.notified_status is Status.DOWN
        assert state.failure_count == 2
        await run_cycles(monitor, 2)
        assert len(telegram_api.messages) == 1  # no duplicate after restart

        sites.set(SITE_A, 200)
        await monitor.run_cycle()
    assert len(telegram_api.messages) == 2
    assert telegram_api.messages[1].startswith("🟢 SERVICE RECOVERED")


async def test_internal_error_in_one_service_does_not_stop_others(
    sites: ScriptedSites, telegram_api: TelegramRecorder, store: StateStore, make_monitor, caplog
) -> None:
    sites.set(SITE_B, 200)  # SITE_A has no script -> KeyError inside the mock transport
    monitor = make_monitor(store)
    await monitor.load_state()
    with caplog.at_level(logging.ERROR):
        await monitor.run_cycle()
    assert monitor.states["Site B"].status is Status.UP
    assert any("internal error" in r.message for r in caplog.records)
