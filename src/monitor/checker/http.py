"""HTTP health checks. The HTTP status code is the only health criterion."""

import asyncio
import socket
import ssl
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http import HTTPStatus

import httpx

from monitor.models import CheckOutcome, CheckResult


def status_text(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return str(code)


def format_seconds(seconds: float) -> str:
    return f"{seconds:g}s"


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def describe_network_error(exc: BaseException) -> str:
    """Turn an httpx/OS exception into a short human-readable reason."""
    chain = list(_exception_chain(exc))
    detail = next((str(e) for e in chain if str(e)), type(exc).__name__)

    if any(isinstance(e, ssl.SSLError) for e in chain) or "CERTIFICATE" in detail.upper():
        return f"TLS/SSL error: {detail}"
    if any(isinstance(e, socket.gaierror) for e in chain) or any(
        marker in detail.lower()
        for marker in ("name or service not known", "getaddrinfo failed", "nodename nor servname")
    ):
        return f"DNS resolution failed: {detail}"
    if any(isinstance(e, ConnectionRefusedError) for e in chain) or "refused" in detail.lower():
        return "connection refused"
    if any(isinstance(e, ConnectionResetError) for e in chain) or "reset" in detail.lower():
        return "connection reset by peer"
    if isinstance(exc, httpx.RemoteProtocolError):
        return f"server closed connection unexpectedly: {detail}"
    if isinstance(exc, httpx.ConnectError):
        return f"connection failed: {detail}"
    return f"{type(exc).__name__}: {detail}"


class HttpChecker:
    """Performs a single GET against a health endpoint using a shared client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def check(
        self, url: str, *, timeout: float, follow_redirects: bool = True
    ) -> CheckResult:
        checked_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            # httpx timeouts are per phase; asyncio.timeout bounds the whole request.
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "GET", url, timeout=timeout, follow_redirects=follow_redirects
                ) as response,
            ):
                # The body is not needed: only the status code matters.
                code = response.status_code
        except (httpx.TimeoutException, TimeoutError):
            return CheckResult(
                outcome=CheckOutcome.FAILURE,
                checked_at=checked_at,
                url=url,
                response_time=time.perf_counter() - started,
                error=f"request timeout after {format_seconds(timeout)}",
            )
        except httpx.TooManyRedirects:
            return CheckResult(CheckOutcome.FAILURE, checked_at, url, error="too many redirects")
        except (httpx.HTTPError, OSError) as exc:
            return CheckResult(
                CheckOutcome.FAILURE, checked_at, url, error=describe_network_error(exc)
            )

        elapsed = time.perf_counter() - started
        if 200 <= code < 300:
            outcome, error = CheckOutcome.SUCCESS, None
        elif code >= 500:
            outcome, error = CheckOutcome.FAILURE, f"HTTP {code}"
        else:
            # 4xx: the server answers, the health URL/auth is likely misconfigured.
            # Final 3xx: redirects disabled or a redirect without Location.
            outcome, error = CheckOutcome.IGNORED, f"HTTP {code}"
        return CheckResult(outcome, checked_at, url, code, elapsed, error)
