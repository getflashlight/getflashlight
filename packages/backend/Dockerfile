FROM python:3.14-slim AS base

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy backend package
COPY packages/backend/ packages/backend/

# Install dependencies
RUN cd packages/backend && uv sync --no-dev --no-editable

# Put uv-managed scripts on PATH so entry points work without "uv run"
ENV PATH="/app/packages/backend/.venv/bin:$PATH"

# Expose server port
EXPOSE 8000

# Run the server
CMD ["uv", "run", "--project", "packages/backend", "auralake-server"]
