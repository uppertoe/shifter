# syntax=docker/dockerfile:1.7
# Multi-stage build:
#   stage 1 (builder): uv-based image, restore deps from uv.lock
#   stage 2 (runtime): bare python-slim, copy only the venv + source
#
# Why python-slim and not Alpine: Python wheels target glibc, so Alpine forces
# source builds for pydantic-core / uvloop / watchfiles, which is much slower
# *and* often produces a larger image. python:3.12-slim-bookworm is the modern
# default for Python services.

# --------------------------------------------------------------------------- #
# Stage 1: build the venv with uv
# --------------------------------------------------------------------------- #
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copy lock + manifest first so this layer caches as long as deps don't change.
COPY pyproject.toml uv.lock ./

# BuildKit cache mount keeps uv's HTTP cache between rebuilds — without bloating
# the resulting image layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy app source and reinstall (now with the project itself).
COPY src/ ./src/
COPY scripts/ ./scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --------------------------------------------------------------------------- #
# Stage 2: minimal runtime
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATABASE_PATH=/data/shifter.db \
    SCREENSHOT_DIR=/data/screenshots

# Run as a non-root user; uid 1000 lines up with the typical first user on
# Linux hosts so bind-mounted ./data won't have permission issues.
RUN groupadd --system --gid 1000 shifter \
 && useradd --system --uid 1000 --gid shifter --no-create-home --shell /usr/sbin/nologin shifter \
 && install -d -o shifter -g shifter /data

WORKDIR /app
COPY --from=builder --chown=shifter:shifter /app/.venv /app/.venv
COPY --from=builder --chown=shifter:shifter /app/src /app/src
COPY --from=builder --chown=shifter:shifter /app/scripts /app/scripts

USER shifter
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=4).read() == b'ok' else 1)"

CMD ["uvicorn", "shifter.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
