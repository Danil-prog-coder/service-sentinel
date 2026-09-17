import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

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


class BotApi:
    """MockTransport handler emulating the Bot API methods used by the control bot.

    ``queue()`` puts updates into the next ``getUpdates`` answer; everything the bot
    sends back is recorded instead of being delivered anywhere.
    """

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.commands: list[dict[str, str]] = []
        self.get_updates_calls: list[dict[str, Any]] = []
        self._next_message_id = 100
        self._update_id = 0

    # --- test helpers ---------------------------------------------------------

    def queue(self, update: dict[str, Any]) -> dict[str, Any]:
        self._update_id += 1
        update.setdefault("update_id", self._update_id)
        self.pending.append(update)
        return update

    @property
    def texts(self) -> list[str]:
        return [message["text"] for message in self.sent]

    @property
    def last_text(self) -> str:
        return (self.edits or self.sent)[-1]["text"]

    def last_keyboard(self) -> list[list[dict[str, str]]]:
        markup = (self.edits or self.sent)[-1].get("reply_markup") or {}
        return markup.get("inline_keyboard", [])

    def buttons(self) -> list[str]:
        return [b["text"] for row in self.last_keyboard() for b in row]

    def callback_data(self, text_startswith: str) -> str:
        for row in self.last_keyboard():
            for item in row:
                if item["text"].startswith(text_startswith):
                    return item["callback_data"]
        raise AssertionError(f"no button starting with {text_startswith!r} in {self.buttons()}")

    # --- the fake API ---------------------------------------------------------

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        handler = getattr(self, f"_{method}", None)
        if handler is None:
            raise AssertionError(f"unexpected Bot API method: {method}")
        result = handler(payload)
        if method == "getUpdates" and not result:
            # Emulate long polling, so a polling loop under test does not spin.
            await asyncio.sleep(min(float(payload.get("timeout", 0)), 0.02))
        return httpx.Response(200, json={"ok": True, "result": result})

    def _getUpdates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self.get_updates_calls.append(payload)
        offset = payload.get("offset")
        if offset == -1:
            return self.pending[-1:]
        updates = [u for u in self.pending if offset is None or u["update_id"] >= offset]
        self.pending = []
        return updates

    def _sendMessage(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        self._next_message_id += 1
        return {"message_id": self._next_message_id}

    def _editMessageText(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.edits.append(payload)
        return {"message_id": payload["message_id"]}

    def _answerCallbackQuery(self, payload: dict[str, Any]) -> bool:
        self.answers.append(payload)
        return True

    def _setMyCommands(self, payload: dict[str, Any]) -> bool:
        self.commands = payload["commands"]
        return True


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
def bot_api() -> BotApi:
    return BotApi()


@pytest.fixture
async def bot_client(bot_api: BotApi) -> AsyncIterator[TelegramClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(bot_api)) as client:
        yield TelegramClient(client, TOKEN, CHAT_ID, sleep=_no_sleep, max_attempts=1)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "monitor.db"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[StateStore]:
    async with StateStore(db_path) as s:
        yield s
