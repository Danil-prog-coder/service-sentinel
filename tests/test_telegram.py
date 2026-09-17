import json
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import CHAT_ID, TOKEN, TelegramRecorder
from monitor.config import ServiceConfig
from monitor.models import ServiceState, Status
from monitor.registry import ManagedService, ServiceSource
from monitor.telegram import TelegramClient, TelegramError
from monitor.telegram.formatting import (
    format_down,
    format_duration,
    format_recovered,
    format_server_warning,
)
from monitor.telegram.ui import card_view, format_interval

MSK = ZoneInfo("Europe/Moscow")


async def test_send_message_payload(
    telegram_api: TelegramRecorder, make_telegram: Callable[..., TelegramClient]
) -> None:
    await make_telegram().send("hello")
    request = telegram_api.requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == CHAT_ID
    assert body["text"] == "hello"
    assert "parse_mode" not in body


async def test_retries_on_server_error(
    telegram_api: TelegramRecorder, make_telegram: Callable[..., TelegramClient]
) -> None:
    telegram_api.fail_next = 2
    await make_telegram(max_attempts=3).send("hello")
    assert telegram_api.messages == ["hello"]
    assert len(telegram_api.requests) == 3


async def test_gives_up_after_max_attempts(
    telegram_api: TelegramRecorder, make_telegram: Callable[..., TelegramClient]
) -> None:
    telegram_api.fail_next = 5
    with pytest.raises(TelegramError, match="after 3 attempts") as info:
        await make_telegram(max_attempts=3).send("hello")
    assert TOKEN not in str(info.value)


async def test_client_error_is_not_retried(
    telegram_api: TelegramRecorder, make_telegram: Callable[..., TelegramClient]
) -> None:
    telegram_api.fail_next = 5
    telegram_api.fail_status = 400
    with pytest.raises(TelegramError, match="rejected"):
        await make_telegram(max_attempts=3).send("hello")
    assert len(telegram_api.requests) == 1


async def test_rate_limit_uses_retry_after() -> None:
    delays: list[float] = []
    responses = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}}),
        httpx.Response(200, json={"ok": True, "result": {}}),
    ]

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    transport = httpx.MockTransport(lambda r: responses.pop(0))
    async with httpx.AsyncClient(transport=transport) as client:
        await TelegramClient(client, TOKEN, CHAT_ID, sleep=fake_sleep).send("hi")
    assert delays == [7.0]


async def test_network_error_message_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to reach {request.url}")

    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelegramError) as info:
            await TelegramClient(client, TOKEN, CHAT_ID, sleep=no_sleep).send("hi")
    assert TOKEN not in str(info.value)
    assert TOKEN not in repr(TelegramClient(client, TOKEN, CHAT_ID))


def test_format_down_http_error() -> None:
    state = ServiceState(
        name="Tatiana Website",
        url="https://example.ru",
        status=Status.DOWN,
        status_since=datetime(2026, 9, 17, 8, 20, tzinfo=UTC),
        failure_count=2,
        last_error="HTTP 502",
        last_status_code=502,
        response_time=1.4234,
    )
    text = format_down(state, "https://example.ru", "https://example.ru", MSK)
    assert text == (
        "🔴 SERVICE DOWN\n\n"
        "Service: Tatiana Website\n"
        "URL: https://example.ru\n"
        "Status: 502 Bad Gateway\n"
        "Error: HTTP 502\n"
        "Response time: 1.42s\n"
        "Failed checks in a row: 2\n"
        "Detected at: 2026-09-17 11:20 MSK"
    )


def test_format_recovered() -> None:
    state = ServiceState(
        name="Tatiana Website",
        url="https://example.ru/health",
        status=Status.UP,
        status_since=datetime(2026, 9, 17, 8, 33, tzinfo=UTC),
        last_status_code=200,
    )
    text = format_recovered(state, "https://example.ru", "https://example.ru/health", MSK, 780)
    assert text == (
        "🟢 SERVICE RECOVERED\n\n"
        "Service: Tatiana Website\n"
        "URL: https://example.ru\n"
        "Health check: https://example.ru/health\n"
        "Status: 200 OK\n"
        "Downtime: 13 minutes\n"
        "Recovered at: 2026-09-17 11:33 MSK"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "n/a"),
        (42, "42 seconds"),
        (60, "1 minute"),
        (780, "13 minutes"),
        (3 * 3600 + 5 * 60, "3 h 5 min"),
        (26 * 3600, "1 d 2 h 0 min"),
    ],
)
def test_format_duration(seconds: float | None, text: str) -> None:
    assert format_duration(seconds) == text


def test_format_server_warning() -> None:
    text = format_server_warning("production-01", [("RAM", 94.2), ("Disk", 71), ("CPU", 48)])
    assert text == "🟠 SERVER WARNING\n\nServer: production-01\nRAM: 94%\nDisk: 71%\nCPU: 48%"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(30, "30 секунд"), (60, "1 минута"), (120, "2 минуты"), (600, "10 минут"), (7200, "2 часа")],
)
def test_format_interval_russian(seconds: float, text: str) -> None:
    assert format_interval(seconds) == text


def test_card_of_a_site_that_was_never_checked() -> None:
    service = ManagedService(
        ServiceConfig(name="New", url="https://new.test"), ServiceSource.TELEGRAM
    )
    text, markup = card_view(service, None, MSK)
    assert text.startswith("⚪ New")
    assert "Статус: ⚪ ещё не проверялся" in text
    assert [b["text"] for row in markup["inline_keyboard"] for b in row] == [
        "🔄 Проверить",
        "⏸ Пауза",
        "🗑 Удалить",
        "« Назад",
    ]
