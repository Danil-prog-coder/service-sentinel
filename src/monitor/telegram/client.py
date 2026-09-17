"""Minimal Telegram Bot API client (sendMessage only) on top of httpx."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


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

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._api_base}/bot{self._token}/{method}"
        last_error = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            delay = self._backoff * 2 ** (attempt - 1)
            try:
                response = await self._client.post(url, json=payload, timeout=self._timeout)
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

            if attempt < self._max_attempts:
                logger.warning(
                    "telegram request failed, retrying",
                    extra={"method": method, "attempt": attempt, "error": last_error},
                )
                await self._sleep(delay)
        raise TelegramError(
            f"Telegram {method} failed after {self._max_attempts} attempts: {last_error}"
        )

    async def send(self, text: str) -> None:
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[: MAX_MESSAGE_LENGTH - 1] + "…"
        await self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
        )

    async def get_updates(self) -> list[dict[str, Any]]:
        result = await self._call("getUpdates", {"allowed_updates": ["message", "my_chat_member"]})
        return list(result or [])


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
