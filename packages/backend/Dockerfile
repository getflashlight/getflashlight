FROM python:3.12-slim AS base

WORKDIR /app

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Backend is a self-contained package — copy just it.
COPY packages/backend/ packages/backend/

RUN cd packages/backend && uv sync --no-dev --no-editable

# Put the venv's console scripts on PATH.
ENV PATH="/app/packages/backend/.venv/bin:$PATH"

EXPOSE 8000 8002

CMD ["auralake-server"]
