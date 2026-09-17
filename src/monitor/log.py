"""Structured logging on top of the standard library, with secret masking.

Extra fields are passed via ``logger.info("msg", extra={"service": ...})``.
"""

import io
import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

# Telegram bot tokens look like "123456789:AA...". Masked even if not registered explicitly.
# No leading \b: in URLs the token directly follows "bot" (".../bot123:AA...").
_TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")
_MASK = "***"

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)


class SecretMasker:
    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        # Very short values are not masked: they would corrupt unrelated text.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _MASK)
        return _TELEGRAM_TOKEN_RE.sub(_MASK, text)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: v.value if isinstance(v, Enum) else v
        for k, v in vars(record).items()
        if k not in _STANDARD_ATTRS
    }


class JsonFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker) -> None:
        super().__init__()
        self._masker = masker

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extra_fields(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return self._masker.mask(json.dumps(payload, ensure_ascii=False, default=str))


class TextFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self._masker = masker

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        extras = _extra_fields(record)
        if extras:
            text += " " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        return self._masker.mask(text)


def configure_logging(level: str, fmt: str, secrets: Iterable[str | None] = ()) -> None:
    masker = SecretMasker(secrets)
    # Alerts contain emoji; don't crash on non-UTF-8 consoles (e.g. Windows cp1251).
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter(masker) if fmt == "text" else JsonFormatter(masker))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    valid_level = level in logging.getLevelNamesMapping()
    root.setLevel(level if valid_level else logging.INFO)
    if not valid_level:
        logging.getLogger(__name__).warning("unknown LOG_LEVEL, using INFO", extra={"value": level})

    # httpx logs every request URL at INFO; Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
