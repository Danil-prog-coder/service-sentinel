import socket
import ssl

import httpx
import pytest

from monitor.checker import HttpChecker
from monitor.checker.http import describe_network_error
from monitor.models import CheckOutcome

URL = "https://site.test/health"


async def check_with(handler: object, **kwargs: object) -> object:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:  # type: ignore[arg-type]
        return await HttpChecker(client).check(URL, timeout=kwargs.pop("timeout", 5.0), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, CheckOutcome.SUCCESS),
        (204, CheckOutcome.SUCCESS),
        (400, CheckOutcome.IGNORED),
        (401, CheckOutcome.IGNORED),
        (404, CheckOutcome.IGNORED),
        (500, CheckOutcome.FAILURE),
        (502, CheckOutcome.FAILURE),
        (503, CheckOutcome.FAILURE),
        (504, CheckOutcome.FAILURE),
    ],
)
async def test_status_classification(status: int, outcome: CheckOutcome) -> None:
    result = await check_with(lambda r: httpx.Response(status))
    assert result.outcome is outcome
    assert result.status_code == status
    assert result.response_time is not None
    if outcome is CheckOutcome.SUCCESS:
        assert result.error is None
    else:
        assert result.error == f"HTTP {status}"


async def test_body_is_not_interpreted() -> None:
    result = await check_with(lambda r: httpx.Response(200, json={"status": "whatever"}))
    assert result.outcome is CheckOutcome.SUCCESS


async def test_redirect_is_followed_to_final_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(301, headers={"Location": "https://site.test/new-health"})
        return httpx.Response(503)

    result = await check_with(handler)
    assert result.outcome is CheckOutcome.FAILURE
    assert result.status_code == 503


async def test_redirect_not_followed_when_disabled() -> None:
    result = await check_with(
        lambda r: httpx.Response(302, headers={"Location": "/x"}), follow_redirects=False
    )
    assert result.outcome is CheckOutcome.IGNORED


async def test_redirect_loop_is_failure() -> None:
    result = await check_with(lambda r: httpx.Response(302, headers={"Location": URL}))
    assert result.outcome is CheckOutcome.FAILURE
    assert result.error == "too many redirects"


async def test_timeout_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = await check_with(handler, timeout=5.0)
    assert result.outcome is CheckOutcome.FAILURE
    assert result.status_code is None
    assert result.error == "request timeout after 5s"


def _raise_from(cause: BaseException, message: str = "") -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(message or str(cause), request=request) from cause

    return handler


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (ConnectionRefusedError(111, "Connection refused"), "connection refused"),
        (ConnectionResetError(104, "Connection reset by peer"), "connection reset by peer"),
        (socket.gaierror(-2, "Name or service not known"), "DNS resolution failed"),
        (ssl.SSLCertVerificationError("certificate verify failed"), "TLS/SSL error"),
    ],
)
async def test_network_errors_are_failures(cause: BaseException, expected: str) -> None:
    result = await check_with(_raise_from(cause))
    assert result.outcome is CheckOutcome.FAILURE
    assert result.error is not None
    assert result.error.startswith(expected)


async def test_remote_protocol_error_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    result = await check_with(handler)
    assert result.outcome is CheckOutcome.FAILURE
    assert "server closed connection" in (result.error or "")


def test_describe_unknown_error() -> None:
    assert describe_network_error(httpx.ReadError("")) == "ReadError: ReadError"
