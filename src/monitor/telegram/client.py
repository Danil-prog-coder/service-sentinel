"""Minimal Telegram Bot API client on top of httpx: alerts and the control bot."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096
ALLOWED_UPDATES = ("message", "callback_query", "my_chat_member")


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._token = token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._sleep = sleep

    def __repr__(self) -> str:  # never expose the token
        return f"TelegramClient(chat_id={self._chat_id!r})"

    @property
    def chat_id(self) -> str:
        return self._chat_id

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        url = f"{self._api_base}/bot{self._token}/{method}"
        attempts = self._max_attempts if max_attempts is None else max_attempts
        last_error = "unknown error"
        for attempt in range(1, attempts + 1):
            delay = self._backoff * 2 ** (attempt - 1)
            try:
                response = await self._client.post(
                    url, json=payload, timeout=timeout or self._timeout
                )
            except httpx.HTTPError as exc:
                # Deliberately no str(exc): some httpx errors embed the request URL.
                last_error = f"network error: {type(exc).__name__}"
            else:
                data = _json(response)
                if response.status_code == 200 and data.get("ok"):
                    return data.get("result")
                description = data.get("description") or response.reason_phrase
                last_error = f"HTTP {response.status_code}: {description}"
                if response.status_code == 429:
                    retry_after = (data.get("parameters") or {}).get("retry_after")
                    if isinstance(retry_after, int | float):
                        delay = min(float(retry_after), 60.0)
                elif response.status_code < 500:
                    raise TelegramError(f"Telegram API rejected {method}: {last_error}")

            if attempt < attempts:
                logger.warning(
                    "telegram request failed, retrying",
                    extra={"method": method, "attempt": attempt, "error": last_error},
                )
                await self._sleep(delay)
        raise TelegramError(f"Telegram {method} failed after {attempts} attempts: {last_error}")

    async def send(self, text: str) -> None:
        """The :class:`Notifier` interface used for alerts."""
        await self.send_message(text)

    async def send_message(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        chat_id: str | int | None = None,
    ) -> int:
        """Send a message and return its ``message_id``."""
        payload: dict[str, Any] = {
            "chat_id": self._chat_id if chat_id is None else chat_id,
            "text": _clip(text),
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = await self._call("sendMessage", payload)
        message_id = (result or {}).get("message_id")
        return int(message_id) if message_id is not None else 0

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit a message in place. "Not modified" (the user pressed the same button) is ignored."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": _clip(text),
            "link_preview_options": {"is_disabled": True},
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        try:
            await self._call("editMessageText", payload, max_attempts=1)
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text[:200]
            payload["show_alert"] = show_alert
        try:
            await self._call("answerCallbackQuery", payload, max_attempts=1)
        except TelegramError as exc:
            # The query expires after ~15s; failing to answer must not break the handler.
            logger.warning("failed to answer callback query", extra={"error": str(exc)})

    async def set_my_commands(self, commands: Sequence[tuple[str, str]]) -> None:
        await self._call(
            "setMyCommands",
            {"commands": [{"command": name, "description": text} for name, text in commands]},
        )

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: float = 0.0,
        allowed_updates: Sequence[str] = ALLOWED_UPDATES,
        max_attempts: int | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "allowed_updates": list(allowed_updates),
            "timeout": int(timeout),
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call(
            "getUpdates",
            payload,
            # Long polling holds the connection open for `timeout` seconds.
            timeout=self._timeout + timeout,
            max_attempts=max_attempts,
        )
        return list(result or [])


def _clip(text: str) -> str:
    if len(text) > MAX_MESSAGE_LENGTH:
        return text[: MAX_MESSAGE_LENGTH - 1] + "…"
    return text


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class DryRunNotifier:
    """Logs messages instead of sending them (``--dry-run`` / local testing)."""

    async def send(self, text: str) -> None:
        logger.info("dry-run notification (not sent)", extra={"text": text})
