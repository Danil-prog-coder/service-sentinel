"""The monitoring loop: parallel checks, state updates, alerts, persistence.

The service list comes from a :class:`ServiceRegistry`, so sites added or paused from
Telegram are picked up by the next cycle without a restart. Every check (scheduled or
manual) holds a per-service lock, so one site is never checked twice at the same time.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable

from monitor.checker.http import HttpChecker
from monitor.config import MonitorSettings
from monitor.models import CheckOutcome, CheckResult, ServiceState, Status
from monitor.monitoring.state_machine import (
    AlertKind,
    Thresholds,
    apply_check,
    downtime,
    mark_notified,
    pending_alert,
)
from monitor.registry import ManagedService, ServiceRegistry
from monitor.storage import StateStore
from monitor.telegram import Notifier, TelegramError
from monitor.telegram.formatting import format_down, format_recovered

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        settings: MonitorSettings,
        registry: ServiceRegistry,
        checker: HttpChecker,
        store: StateStore,
        notifier: Notifier,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._checker = checker
        self._store = store
        self._notifier = notifier
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._states: dict[str, ServiceState] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def settings(self) -> MonitorSettings:
        return self._settings

    @property
    def registry(self) -> ServiceRegistry:
        return self._registry

    @property
    def states(self) -> dict[str, ServiceState]:
        return self._states

    def state_of(self, name: str) -> ServiceState | None:
        return self._states.get(name)

    async def load_state(self) -> None:
        await self._registry.load()
        for service in self._registry.all():
            state = await self._state(service)
            logger.info(
                "service registered",
                extra={
                    "service": service.name,
                    "url": service.check_url,
                    "source": service.source,
                    "enabled": service.enabled,
                    "status": state.status,
                    "notified_status": state.notified_status,
                },
            )

    async def _state(self, service: ManagedService) -> ServiceState:
        state = self._states.get(service.name)
        if state is None or state.url != service.check_url:
            state = await self._store.load(service.name, service.check_url)
            self._states[service.name] = state
        return state

    # --- runtime changes requested from Telegram -----------------------------------

    async def add_service(self, raw_url: str, name: str | None = None) -> ManagedService:
        service = await self._registry.add(raw_url, name)
        await self._state(service)
        return service

    async def remove_service(self, name: str) -> ManagedService:
        # Wait for a check of this site to finish, so its state is not written back
        # to the database right after we delete it.
        async with self._locks[name]:
            service = await self._registry.remove(name)
        self._states.pop(name, None)
        self._locks.pop(name, None)
        return service

    async def set_enabled(self, name: str, enabled: bool) -> ManagedService:
        return await self._registry.set_enabled(name, enabled)

    # --- checking ------------------------------------------------------------------

    async def run_cycle(self) -> None:
        services = self._registry.enabled()
        logger.info("check cycle started", extra={"services": len(services)})
        loop = asyncio.get_running_loop()
        started = loop.time()
        # One task per enabled service (a bounded, known number);
        # the semaphore limits how many HTTP requests run at once.
        async with asyncio.TaskGroup() as tg:
            for service in services:
                tg.create_task(self._process(service), name=f"check:{service.name}")
        summary: dict[str, int] = {}
        for service in services:
            state = self._states.get(service.name)
            if state is not None:
                summary[state.status] = summary.get(state.status, 0) + 1
        logger.info(
            "check cycle finished",
            extra={"duration": round(loop.time() - started, 3), **summary},
        )

    async def check_service(self, service: ManagedService) -> CheckResult | None:
        """Check one service now and return the raw result (``None`` if it was deleted).

        Paused services are checked on demand but never alert.
        """
        # The lock makes a manual check wait for a running cycle instead of racing it:
        # state, storage and the "already notified" flag stay consistent, so the user
        # never gets a duplicate alert for a check they triggered themselves.
        async with self._locks[service.name]:
            if self._registry.get(service.name) is None:
                logger.info("service was removed, check skipped", extra={"service": service.name})
                return None
            async with self._semaphore:
                result = await self._checker.check(
                    service.check_url,
                    timeout=service.config.timeout or self._settings.request_timeout,
                    follow_redirects=service.config.follow_redirects,
                )
            self._log_result(service, result)

            state = await self._state(service)
            previous = state.status
            thresholds = Thresholds(
                failure=service.config.failure_threshold or self._settings.failure_threshold,
                recovery=service.config.recovery_threshold or self._settings.recovery_threshold,
            )
            new_status = apply_check(state, result, thresholds)
            if new_status is not None:
                logger.log(
                    logging.WARNING if new_status is Status.DOWN else logging.INFO,
                    "service status changed",
                    extra={"service": service.name, "from": previous, "to": new_status},
                )
            await self._store.save(state)
            if service.enabled:
                await self._notify_if_needed(service, state)
            return result

    async def _process(self, service: ManagedService) -> None:
        # Any bug here must not break the other checks or the loop.
        try:
            await self.check_service(service)
        except Exception:
            logger.exception(
                "internal error while processing service", extra={"service": service.name}
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

    async def _notify_if_needed(self, service: ManagedService, state: ServiceState) -> None:
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
    def _log_result(service: ManagedService, result: CheckResult) -> None:
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
