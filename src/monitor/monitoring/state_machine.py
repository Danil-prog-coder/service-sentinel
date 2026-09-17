"""Pure UP/DOWN state logic: no I/O, easy to test.

Alerting is driven by ``notified_status`` (what the user already knows) rather than by
the transition itself. This makes alerts idempotent: a failed Telegram send is retried
on the next cycle, and a restart never produces a duplicate alert.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from monitor.models import CheckOutcome, CheckResult, ServiceState, Status


@dataclass(frozen=True, slots=True)
class Thresholds:
    failure: int
    recovery: int


class AlertKind(StrEnum):
    DOWN = "down"
    RECOVERED = "recovered"


def apply_check(state: ServiceState, result: CheckResult, thresholds: Thresholds) -> Status | None:
    """Update ``state`` in place. Returns the new status if it changed, else ``None``."""
    state.last_check = result.checked_at

    if result.outcome is CheckOutcome.IGNORED:
        return None

    state.last_status_code = result.status_code
    state.response_time = result.response_time
    new_status: Status | None = None

    if result.outcome is CheckOutcome.SUCCESS:
        state.last_success = result.checked_at
        state.failure_count = 0
        state.failing_since = None
        state.recovery_count += 1
        if state.status is Status.UNKNOWN or (
            state.status is Status.DOWN and state.recovery_count >= thresholds.recovery
        ):
            new_status = Status.UP
    else:
        state.last_failure = result.checked_at
        state.last_error = result.error
        state.recovery_count = 0
        state.failure_count += 1
        if state.failing_since is None:
            state.failing_since = result.checked_at
        if state.status is not Status.DOWN and state.failure_count >= thresholds.failure:
            new_status = Status.DOWN
            state.down_since = state.failing_since

    if new_status is not None:
        state.status = new_status
        state.status_since = result.checked_at
    return new_status


def pending_alert(state: ServiceState) -> AlertKind | None:
    """Which alert (if any) the user has not received yet for the current status."""
    if state.status is Status.DOWN and state.notified_status is not Status.DOWN:
        return AlertKind.DOWN
    if state.status is Status.UP and state.notified_status is Status.DOWN:
        return AlertKind.RECOVERED
    return None


def mark_notified(state: ServiceState, kind: AlertKind) -> None:
    if kind is AlertKind.DOWN:
        state.notified_status = Status.DOWN
    else:
        state.notified_status = Status.UP
        state.down_since = None


def downtime(state: ServiceState, until: datetime | None = None) -> float | None:
    """Outage duration in seconds: from the first failed check to recovery (or ``until``)."""
    end = until or state.status_since
    if state.down_since is None or end is None:
        return None
    return max(0.0, (end - state.down_since).total_seconds())
