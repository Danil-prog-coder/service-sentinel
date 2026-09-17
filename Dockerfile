# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    CONFIG_PATH=/app/config.yaml \
    DB_PATH=/app/data/monitor.db \
    HEARTBEAT_PATH=/app/data/heartbeat.json

RUN groupadd --system --gid 10001 monitor \
    && useradd --system --uid 10001 --gid monitor --home-dir /app --shell /usr/sbin/nologin monitor \
    && mkdir -p /app/data \
    && chown monitor:monitor /app/data

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
USER monitor
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "monitor.healthcheck"]

CMD ["python", "-m", "monitor"]
