# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# git is required to resolve any private git+URL dependency (internal libs).
# python:slim ships without it. (ORG-109) Concretely here: `workflow-runtime-core`
# is pinned as a git+https dependency on an exact tag (ORG-PLAN-155 locked
# decision 4), and uv shells out to `git` to fetch it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
# Mint-time auth for private internal libs: the stromy-ci App token is passed
# as a build secret (deploy-aca.yml) and injected via git URL rewrite, scoped to
# this builder stage only. The `if -s` guard keeps a local `docker build` (no
# secret) working for any all-public dependency set. Token lands only in the
# builder's /root/.gitconfig, never in the final image (which COPYs /app only).
RUN --mount=type=secret,id=internal_libs_pat \
    if [ -s /run/secrets/internal_libs_pat ]; then \
        git config --global \
            url."https://x-access-token:$(cat /run/secrets/internal_libs_pat)@github.com/".insteadOf \
            "https://github.com/"; \
    fi; \
    uv sync --frozen --no-dev --no-install-project

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
