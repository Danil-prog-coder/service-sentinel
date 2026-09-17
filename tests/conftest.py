import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from monitor.storage import StateStore
from monitor.telegram import TelegramClient

TOKEN = "123456789:AAtest-token-that-must-never-leak-123456"
CHAT_ID = "-1001234567890"


class ScriptedSites:
    """MockTransport handler: per-URL queue of responses (int status) or exceptions."""

    def __init__(self) -> None:
        self.script: dict[str, list[int | Exception]] = {}
        self.calls: list[str] = []

    def set(self, url: str, *steps: int | Exception) -> None:
        self.script[url] = list(steps)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        steps = self.script[url]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step, json={"status": "ok" if step < 400 else "unhealthy"})


class TelegramRecorder:
    """MockTransport handler emulating the Bot API; never touches the network."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.requests: list[httpx.Request] = []
        self.fail_next = 0
        self.fail_status = 500

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(
                self.fail_status, json={"ok": False, "description": "Internal Server Error"}
            )
        body = json.loads(request.content)
        self.messages.append(body["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.messages)}})


@pytest.fixture
def sites() -> ScriptedSites:
    return ScriptedSites()


@pytest.fixture
def telegram_api() -> TelegramRecorder:
    return TelegramRecorder()


@pytest.fixture
async def site_client(sites: ScriptedSites) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(sites)) as client:
        yield client


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def make_telegram(
    telegram_api: TelegramRecorder,
) -> AsyncIterator[Callable[..., TelegramClient]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_api)) as client:

        def factory(**kwargs: object) -> TelegramClient:
            return TelegramClient(client, TOKEN, CHAT_ID, sleep=_no_sleep, **kwargs)  # type: ignore[arg-type]

        yield factory


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "monitor.db"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[StateStore]:
    async with StateStore(db_path) as s:
        yield s
