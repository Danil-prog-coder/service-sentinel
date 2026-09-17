"""Logging redaction, heartbeat, metrics thresholds, graceful shutdown."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from conftest import TOKEN
from monitor.checker import HttpChecker
from monitor.config import MonitorSettings, ServiceConfig
from monitor.healthcheck import check_heartbeat, write_heartbeat
from monitor.log import JsonFormatter, SecretMasker, TextFormatter
from monitor.metrics import Metrics, ResourceThresholds, find_breaches
from monitor.metrics.thresholds import consecutive_checks_required
from monitor.models import CheckResult
from monitor.monitoring import MonitorService
from monitor.storage import StateStore
from monitor.telegram import DryRunNotifier


def make_record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("monitor", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_masks_secrets_and_keeps_extras() -> None:
    formatter = JsonFormatter(SecretMasker(["super-secret-password"]))
    line = formatter.format(
        make_record(
            f"calling https://api.telegram.org/bot{TOKEN}/sendMessage",
            service="Site",
            status_code=502,
            note="pwd=super-secret-password",
        )
    )
    data = json.loads(line)
    assert TOKEN not in line
    assert "super-secret-password" not in line
    assert data["service"] == "Site"
    assert data["status_code"] == 502
    assert data["level"] == "INFO"
    assert "***" in data["msg"]


def test_text_formatter_masks_token_without_registration() -> None:
    line = TextFormatter(SecretMasker()).format(make_record("token", value=TOKEN))
    assert TOKEN not in line
    assert "value='***'" in line


def test_short_secrets_are_not_masked() -> None:
    assert SecretMasker(["-100"]).mask("chat -100") == "chat -100"


def test_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "hb" / "heartbeat.json"
    assert check_heartbeat(path)[0] is False
    write_heartbeat(path, interval=600)
    assert check_heartbeat(path)[0] is True
    assert check_heartbeat(path, now=time.time() + 600 * 2 + 60)[0] is True
    healthy, message = check_heartbeat(path, now=time.time() + 600 * 3)
    assert healthy is False
    assert "stale" in message


def test_find_breaches() -> None:
    metrics = Metrics(
        collected_at=datetime.now(UTC), cpu_percent=48, ram_percent=94, disk_percent=None
    )
    breaches = find_breaches(metrics, ResourceThresholds())
    assert [(b.resource, b.value, b.limit) for b in breaches] == [("RAM", 94, 90.0)]
    assert find_breaches(metrics, ResourceThresholds(ram_percent=None)) == []


def test_consecutive_checks_required() -> None:
    assert consecutive_checks_required(300, 60) == 5
    assert consecutive_checks_required(300, 600) == 1
    assert consecutive_checks_required(310, 60) == 6


async def test_run_forever_stops_on_signal(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    settings = MonitorSettings(interval=3600, shutdown_timeout=1)
    services = [ServiceConfig(name="A", url="https://a.test/health")]
    beats: list[float] = []
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        StateStore(tmp_path / "db.sqlite") as store,
    ):
        monitor = MonitorService(settings, services, HttpChecker(client), store, DryRunNotifier())
        await monitor.load_state()
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run_forever(stop, lambda: beats.append(time.time())))
        await asyncio.sleep(0.2)  # first cycle done, now sleeping for an hour
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert calls == 1
    assert len(beats) == 1


async def test_shutdown_cancels_stuck_cycle(tmp_path: Path) -> None:
    class SlowChecker(HttpChecker):
        async def check(
            self, url: str, *, timeout: float, follow_redirects: bool = True
        ) -> CheckResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    settings = MonitorSettings(interval=3600, shutdown_timeout=0.2)
    services = [ServiceConfig(name="A", url="https://a.test/health")]
    async with StateStore(tmp_path / "db.sqlite") as store:
        monitor = MonitorService(
            settings,
            services,
            SlowChecker(None),  # type: ignore[arg-type]
            store,
            DryRunNotifier(),
        )
        await monitor.load_state()
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run_forever(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
