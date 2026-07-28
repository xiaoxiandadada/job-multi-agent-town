ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

USER root

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONNOUSERSITE=1 \
    JOB_AGENT_PROJECT_ROOT=/app \
    JOB_AGENT_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml ./
COPY docker/package-readme.md ./README.md
COPY src/job_agent_harness/__init__.py ./src/job_agent_harness/__init__.py

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --retries 10 .

COPY src ./src
COPY configs ./configs
COPY web ./web
COPY README.md ./README.md

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1

RUN (id -u appuser >/dev/null 2>&1 \
    || useradd --create-home --uid 10001 appuser) \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

USER appuser

CMD ["job-agent-feishu"]
