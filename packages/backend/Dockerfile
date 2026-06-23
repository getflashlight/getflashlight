# syntax=docker/dockerfile:1

# ---- builder: resolve deps + build the venv (has uv + build toolchain) ----
FROM python:3.14-slim AS builder

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy + compile bytecode for faster cold starts; copy (not hardlink) into the venv.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 1) Install ONLY dependencies first — this layer is cached until pyproject changes.
COPY packages/backend/pyproject.toml packages/backend/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    cd packages/backend && uv sync --no-dev --no-install-project --no-editable

# 2) Install the project itself (source changes invalidate only this layer).
COPY packages/backend/ packages/backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd packages/backend && uv sync --no-dev --no-editable

# ---- runtime: slim image with just the prebuilt venv (no uv, no build tools) ----
FROM python:3.14-slim AS runtime

WORKDIR /app
COPY --from=builder /app/packages/backend/.venv /app/packages/backend/.venv

# The unified `auralake` console script (serve/ingest/transform/aws) on PATH.
ENV PATH="/app/packages/backend/.venv/bin:$PATH"

EXPOSE 8002

CMD ["auralake", "serve"]
