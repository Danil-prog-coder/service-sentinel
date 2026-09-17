"""Resource thresholds (stage 2).

"CPU > 90% for N minutes" and alert de-duplication reuse the same idea as HTTP checks:
a breached snapshot counts as a failure, and ``failure_threshold`` becomes
``ceil(duration / interval)`` consecutive breached snapshots.
"""

import math
from dataclasses import dataclass

from monitor.metrics.base import Metrics


@dataclass(frozen=True, slots=True)
class ResourceThresholds:
    cpu_percent: float | None = 90.0
    ram_percent: float | None = 90.0
    disk_percent: float | None = 90.0
    cpu_duration: float = 300.0
    """CPU must stay above the limit this long (seconds) before alerting."""


@dataclass(frozen=True, slots=True)
class Breach:
    resource: str
    value: float
    limit: float


def find_breaches(metrics: Metrics, thresholds: ResourceThresholds) -> list[Breach]:
    checks = (
        ("CPU", metrics.cpu_percent, thresholds.cpu_percent),
        ("RAM", metrics.ram_percent, thresholds.ram_percent),
        ("Disk", metrics.disk_percent, thresholds.disk_percent),
    )
    return [
        Breach(name, value, limit)
        for name, value, limit in checks
        if value is not None and limit is not None and value > limit
    ]


def consecutive_checks_required(duration: float, interval: float) -> int:
    return max(1, math.ceil(duration / interval))
