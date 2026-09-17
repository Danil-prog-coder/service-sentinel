"""Plain-text Telegram messages (no parse_mode, so no escaping issues)."""

from collections.abc import Sequence
from datetime import datetime, tzinfo

from monitor.checker.http import status_text
from monitor.models import ServiceState


def format_time(value: datetime | None, tz: tzinfo) -> str:
    if value is None:
        return "n/a"
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    total = round(seconds)
    if total < 60:
        return f"{total} seconds"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h {minutes} min"


def _header(title: str, name: str, url: str, check_url: str) -> list[str]:
    lines = [title, "", f"Service: {name}", f"URL: {url}"]
    if check_url != url:
        lines.append(f"Health check: {check_url}")
    return lines


def format_down(state: ServiceState, url: str, check_url: str, tz: tzinfo) -> str:
    lines = _header("🔴 SERVICE DOWN", state.name, url, check_url)
    if state.last_status_code is not None:
        lines.append(f"Status: {status_text(state.last_status_code)}")
    lines.append(f"Error: {state.last_error or 'unknown error'}")
    if state.last_status_code is not None and state.response_time is not None:
        lines.append(f"Response time: {state.response_time:.2f}s")
    if state.failure_count > 1:
        lines.append(f"Failed checks in a row: {state.failure_count}")
    lines.append(f"Detected at: {format_time(state.status_since, tz)}")
    return "\n".join(lines)


def format_recovered(
    state: ServiceState, url: str, check_url: str, tz: tzinfo, downtime: float | None
) -> str:
    lines = _header("🟢 SERVICE RECOVERED", state.name, url, check_url)
    if state.last_status_code is not None:
        lines.append(f"Status: {status_text(state.last_status_code)}")
    lines += [
        f"Downtime: {format_duration(downtime)}",
        f"Recovered at: {format_time(state.status_since, tz)}",
    ]
    return "\n".join(lines)


def format_server_warning(server: str, values: Sequence[tuple[str, float]]) -> str:
    """Stage 2: resource alert, e.g. ``[("RAM", 94.0), ("Disk", 71.0)]``."""
    return "\n".join(
        ["🟠 SERVER WARNING", "", f"Server: {server}"]
        + [f"{label}: {value:.0f}%" for label, value in values]
    )


def format_server_recovered(server: str, values: Sequence[tuple[str, float]]) -> str:
    return "\n".join(
        ["🟢 SERVER OK", "", f"Server: {server}"]
        + [f"{label}: {value:.0f}%" for label, value in values]
    )
