ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONNOUSERSITE=1 \
    JOB_AGENT_PROJECT_ROOT=/app \
    JOB_AGENT_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
COPY web ./web

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN (id -u appuser >/dev/null 2>&1 \
    || useradd --create-home --uid 10001 appuser) \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

USER appuser

CMD ["job-agent-feishu"]
