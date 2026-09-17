from datetime import UTC, datetime, timedelta

from monitor.models import CheckOutcome, CheckResult, ServiceState, Status
from monitor.monitoring.state_machine import (
    AlertKind,
    Thresholds,
    apply_check,
    downtime,
    mark_notified,
    pending_alert,
)

T0 = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)
URL = "https://site.test/health"
TH = Thresholds(failure=2, recovery=1)


def ok(minute: int) -> CheckResult:
    return CheckResult(CheckOutcome.SUCCESS, T0 + timedelta(minutes=minute), URL, 200, 0.1)


def fail(minute: int, code: int | None = 502) -> CheckResult:
    return CheckResult(
        CheckOutcome.FAILURE, T0 + timedelta(minutes=minute), URL, code, 1.4, f"HTTP {code}"
    )


def client_error(minute: int) -> CheckResult:
    return CheckResult(
        CheckOutcome.IGNORED, T0 + timedelta(minutes=minute), URL, 404, 0.1, "HTTP 404"
    )


def new_state() -> ServiceState:
    return ServiceState(name="Site", url=URL)


def test_first_success_is_up_without_alert() -> None:
    state = new_state()
    assert apply_check(state, ok(0), TH) is Status.UP
    assert state.status_since == T0
    assert state.last_success == T0
    assert pending_alert(state) is None


def test_single_failure_is_not_down() -> None:
    state = new_state()
    apply_check(state, ok(0), TH)
    assert apply_check(state, fail(10), TH) is None
    assert state.status is Status.UP
    assert state.failure_count == 1
    assert pending_alert(state) is None


def test_random_failure_then_success_resets_counter() -> None:
    state = new_state()
    apply_check(state, ok(0), TH)
    apply_check(state, fail(10), TH)
    apply_check(state, ok(20), TH)
    apply_check(state, fail(30), TH)
    assert state.status is Status.UP
    assert state.failure_count == 1


def test_consecutive_failures_mark_down() -> None:
    state = new_state()
    apply_check(state, ok(0), TH)
    apply_check(state, fail(10), TH)
    assert apply_check(state, fail(20), TH) is Status.DOWN
    assert state.down_since == T0 + timedelta(minutes=10)  # first failure of the streak
    assert state.status_since == T0 + timedelta(minutes=20)
    assert state.last_error == "HTTP 502"
    assert pending_alert(state) is AlertKind.DOWN


def test_unknown_to_down_alerts() -> None:
    state = new_state()
    apply_check(state, fail(0, None), TH)
    apply_check(state, fail(10, None), TH)
    assert state.status is Status.DOWN
    assert pending_alert(state) is AlertKind.DOWN


def test_no_repeated_down_alert() -> None:
    state = new_state()
    for minute in (0, 10):
        apply_check(state, fail(minute), TH)
    assert pending_alert(state) is AlertKind.DOWN
    mark_notified(state, AlertKind.DOWN)
    for minute in range(20, 200, 10):
        assert apply_check(state, fail(minute), TH) is None
        assert pending_alert(state) is None
    assert state.failure_count == 20


def test_down_alert_retried_until_marked() -> None:
    state = new_state()
    for minute in (0, 10, 20):
        apply_check(state, fail(minute), TH)
    # Telegram failed twice: alert stays pending.
    assert pending_alert(state) is AlertKind.DOWN
    apply_check(state, fail(30), TH)
    assert pending_alert(state) is AlertKind.DOWN


def test_recovery_after_down() -> None:
    state = new_state()
    apply_check(state, ok(0), TH)
    apply_check(state, fail(10), TH)
    apply_check(state, fail(20), TH)
    mark_notified(state, AlertKind.DOWN)

    assert apply_check(state, ok(23), TH) is Status.UP
    assert pending_alert(state) is AlertKind.RECOVERED
    assert downtime(state) == 13 * 60
    mark_notified(state, AlertKind.RECOVERED)
    assert state.notified_status is Status.UP
    assert state.down_since is None
    assert pending_alert(state) is None

    apply_check(state, ok(30), TH)
    assert pending_alert(state) is None


def test_recovery_threshold() -> None:
    th = Thresholds(failure=1, recovery=3)
    state = new_state()
    apply_check(state, fail(0), th)
    mark_notified(state, AlertKind.DOWN)
    assert apply_check(state, ok(10), th) is None
    assert apply_check(state, ok(20), th) is None
    assert state.status is Status.DOWN
    assert apply_check(state, ok(30), th) is Status.UP


def test_failure_during_recovery_resets_recovery_count() -> None:
    th = Thresholds(failure=1, recovery=2)
    state = new_state()
    apply_check(state, fail(0), th)
    apply_check(state, ok(10), th)
    apply_check(state, fail(20), th)
    assert state.recovery_count == 0
    apply_check(state, ok(30), th)
    assert state.status is Status.DOWN


def test_client_error_does_not_change_state() -> None:
    state = new_state()
    apply_check(state, ok(0), TH)
    for minute in (10, 20, 30):
        assert apply_check(state, client_error(minute), TH) is None
    assert state.status is Status.UP
    assert state.failure_count == 0
    assert state.last_check == T0 + timedelta(minutes=30)
    assert state.last_status_code == 200
    assert pending_alert(state) is None


def test_client_error_does_not_break_failure_streak() -> None:
    state = new_state()
    apply_check(state, ok(0), TH)
    apply_check(state, fail(10), TH)
    apply_check(state, client_error(20), TH)
    assert apply_check(state, fail(30), TH) is Status.DOWN


def test_recovered_without_down_notification_is_silent() -> None:
    state = new_state()
    apply_check(state, fail(0), TH)
    apply_check(state, fail(10), TH)
    # DOWN alert never delivered, service came back.
    apply_check(state, ok(20), TH)
    assert pending_alert(state) is None
