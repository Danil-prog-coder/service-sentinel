"""The monitoring loop: parallel checks, state updates, alerts, persistence."""

import asyncio
import logging
from collections.abc import Callable

from monitor.checker.http import HttpChecker
from monitor.config import MonitorSettings, ServiceConfig
from monitor.models import CheckOutcome, CheckResult, ServiceState, Status
from monitor.monitoring.state_machine import (
    AlertKind,
    Thresholds,
    apply_check,
    downtime,
    mark_notified,
    pending_alert,
)
from monitor.storage import StateStore
from monitor.telegram import Notifier, TelegramError
from monitor.telegram.formatting import format_down, format_recovered

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        settings: MonitorSettings,
        services: list[ServiceConfig],
        checker: HttpChecker,
        store: StateStore,
        notifier: Notifier,
    ) -> None:
        self._settings = settings
        self._services = [s for s in services if s.enabled]
        self._checker = checker
        self._store = store
        self._notifier = notifier
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._states: dict[str, ServiceState] = {}

    @property
    def states(self) -> dict[str, ServiceState]:
        return self._states

    async def load_state(self) -> None:
        for service in self._services:
            state = await self._store.load(service.name, service.check_url)
            self._states[service.name] = state
            logger.info(
                "service registered",
                extra={
                    "service": service.name,
                    "url": service.check_url,
                    "status": state.status,
                    "notified_status": state.notified_status,
                },
            )

    async def run_cycle(self) -> None:
        logger.info("check cycle started", extra={"services": len(self._services)})
        loop = asyncio.get_running_loop()
        started = loop.time()
        # One task per configured service (a bounded, known number);
        # the semaphore limits how many HTTP requests run at once.
        async with asyncio.TaskGroup() as tg:
            for service in self._services:
                tg.create_task(self._process(service), name=f"check:{service.name}")
        summary: dict[str, int] = {}
        for state in self._states.values():
            summary[state.status] = summary.get(state.status, 0) + 1
        logger.info(
            "check cycle finished",
            extra={"duration": round(loop.time() - started, 3), **summary},
        )

    async def run_forever(
        self, stop: asyncio.Event, on_cycle_done: Callable[[], None] | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            started = loop.time()
            await self._run_cycle_until_stopped(stop)
            if on_cycle_done is not None:
                on_cycle_done()
            if stop.is_set():
                break
            delay = max(0.0, self._settings.interval - (loop.time() - started))
            try:
                async with asyncio.timeout(delay):
                    await stop.wait()
            except TimeoutError:
                pass

    async def _run_cycle_until_stopped(self, stop: asyncio.Event) -> None:
        cycle = asyncio.create_task(self.run_cycle(), name="check-cycle")
        stop_waiter = asyncio.create_task(stop.wait(), name="stop-waiter")
        try:
            await asyncio.wait({cycle, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not cycle.done():
                logger.info(
                    "shutdown requested, waiting for current check cycle",
                    extra={"timeout": self._settings.shutdown_timeout},
                )
                done, _ = await asyncio.wait({cycle}, timeout=self._settings.shutdown_timeout)
                if not done:
                    logger.warning("check cycle did not finish in time, cancelling")
                    cycle.cancel()
                    await asyncio.gather(cycle, return_exceptions=True)
                    return
            if not cycle.cancelled() and (exc := cycle.exception()) is not None:
                logger.error("check cycle crashed", exc_info=exc)
        finally:
            stop_waiter.cancel()

    async def _process(self, service: ServiceConfig) -> None:
        # Any bug here must not break the other checks or the loop.
        try:
            async with self._semaphore:
                result = await self._checker.check(
                    service.check_url,
                    timeout=service.timeout or self._settings.request_timeout,
                    follow_redirects=service.follow_redirects,
                )
            self._log_result(service, result)

            state = self._states[service.name]
            previous = state.status
            thresholds = Thresholds(
                failure=service.failure_threshold or self._settings.failure_threshold,
                recovery=service.recovery_threshold or self._settings.recovery_threshold,
            )
            new_status = apply_check(state, result, thresholds)
            if new_status is not None:
                logger.log(
                    logging.WARNING if new_status is Status.DOWN else logging.INFO,
                    "service status changed",
                    extra={"service": service.name, "from": previous, "to": new_status},
                )
            await self._store.save(state)
            await self._notify_if_needed(service, state)
        except Exception:
            logger.exception(
                "internal error while processing service", extra={"service": service.name}
            )

    async def _notify_if_needed(self, service: ServiceConfig, state: ServiceState) -> None:
        kind = pending_alert(state)
        if kind is None:
            return
        tz = self._settings.tz
        if kind is AlertKind.DOWN:
            text = format_down(state, service.url, service.check_url, tz)
        else:
            text = format_recovered(state, service.url, service.check_url, tz, downtime(state))
            logger.info(
                "service recovered",
                extra={"service": service.name, "downtime": downtime(state)},
            )
        try:
            await self._notifier.send(text)
        except TelegramError as exc:
            logger.error(
                "failed to send notification, will retry next cycle",
                extra={"service": service.name, "alert": kind, "error": str(exc)},
            )
            return
        mark_notified(state, kind)
        await self._store.save(state)
        logger.info("notification sent", extra={"service": service.name, "alert": kind})

    @staticmethod
    def _log_result(service: ServiceConfig, result: CheckResult) -> None:
        extra = {
            "service": service.name,
            "url": result.url,
            "outcome": result.outcome,
            "status_code": result.status_code,
            "response_time": (
                round(result.response_time, 3) if result.response_time is not None else None
            ),
            "error": result.error,
        }
        if result.outcome is CheckOutcome.SUCCESS:
            logger.info("health check ok", extra=extra)
        elif result.outcome is CheckOutcome.IGNORED:
            logger.warning("health check returned client error, no alert", extra=extra)
        elif result.status_code is not None:
            logger.warning("health check failed: server error", extra=extra)
        else:
            logger.warning("health check failed: network error", extra=extra)
