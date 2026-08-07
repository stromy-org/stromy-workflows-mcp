# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# `workflow-runtime-core` is pinned as a git+https dependency on an exact tag
# (ORG-PLAN-155 locked decision 4), and uv shells out to `git` to fetch it.
# python:3.13-slim ships no git binary, so without this the build fails with
# "Git executable not found" — but only once a git-sourced dependency exists,
# which is why this image built fine until Phase A added the first one.
#
# Builder stage only: the runtime stage copies the resolved /app/.venv, so the
# final image still carries no git and no build toolchain.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src/ ./src/
COPY components/ ./components/
COPY skills/ ./skills/
RUN uv sync --frozen --no-dev


FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    FASTMCP_TRANSPORT=streamable-http \
    FASTMCP_HOST=0.0.0.0 \
    FASTMCP_PORT=8080 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["python", "-m", "stromy_workflows_mcp.server"]
