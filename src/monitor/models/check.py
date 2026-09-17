from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CheckOutcome(StrEnum):
    SUCCESS = "success"
    """2xx (after following redirects): the service works."""

    IGNORED = "ignored"
    """4xx or an unexpected final 3xx: logged, but does not affect the service state."""

    FAILURE = "failure"
    """5xx or a network-level error (timeout, refused, reset, DNS, TLS, ...)."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    outcome: CheckOutcome
    checked_at: datetime
    url: str
    status_code: int | None = None
    response_time: float | None = None
    error: str | None = None
