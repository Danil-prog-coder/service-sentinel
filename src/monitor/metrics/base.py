"""Server metrics contract (stage 2).

HTTP monitoring does not depend on this module. Concrete providers (Zabbix API,
own agent, SSH via asyncssh, Timeweb API if it really exposes the metrics) implement
``MetricsProvider`` and are added later without touching the HTTP part.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Server:
    name: str
    host: str
    provider: str
    """Provider key, e.g. ``"zabbix"`` or ``"ssh"``."""
    options: dict[str, str] = field(default_factory=dict)
    """Non-secret provider options (Zabbix host id, SSH user, ...). Secrets come from ENV."""


@dataclass(frozen=True, slots=True)
class ContainerStatus:
    name: str
    state: str
    """E.g. ``running``, ``exited``, ``restarting``."""
    health: str | None = None


@dataclass(frozen=True, slots=True)
class Metrics:
    """Snapshot of server resources. Every field is optional: providers differ."""

    collected_at: datetime
    cpu_percent: float | None = None
    ram_percent: float | None = None
    ram_available_bytes: int | None = None
    disk_percent: float | None = None
    disk_available_bytes: int | None = None
    load_average: tuple[float, float, float] | None = None
    uptime_seconds: float | None = None
    containers: tuple[ContainerStatus, ...] = ()


class MetricsError(Exception):
    """Provider could not collect metrics (unreachable, auth failed, ...)."""


class MetricsProvider(Protocol):
    async def get_metrics(self, server: Server) -> Metrics: ...
