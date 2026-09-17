from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Status(StrEnum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DOWN = "DOWN"


@dataclass(slots=True)
class ServiceState:
    """Persistent state of one monitored service.

    ``last_status_code`` / ``response_time`` / ``last_error`` describe the last
    *conclusive* check (success or failure); ignored 4xx responses only touch
    ``last_check``.
    """

    name: str
    url: str
    status: Status = Status.UNKNOWN
    status_since: datetime | None = None
    failure_count: int = 0
    recovery_count: int = 0
    failing_since: datetime | None = None
    """Timestamp of the first failed check in the current failure streak."""
    down_since: datetime | None = None
    last_check: datetime | None = None
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error: str | None = None
    last_status_code: int | None = None
    response_time: float | None = None
    notified_status: Status | None = None
    """Last status the user was successfully told about via Telegram."""
