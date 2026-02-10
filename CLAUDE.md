# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Auralake

Auralake is a multi-platform lakehouse cost optimization tool. It analyzes Databricks (and potentially other lakehouse providers) usage, billing, compute, and job data to generate cost-saving recommendations. It can apply changes via Databricks Asset Bundle (DAB) modifications and submit them as GitHub pull requests.

## Monorepo Structure

This is a **uv workspace** monorepo with 2 Python packages:

```
packages/
  backend/    → auralake-backend  (FastAPI server, analyzers, actions, concrete providers)
                also ships auralake_shared (core, models, provider ABCs) as a sub-package
  cli/        → auralake-cli      (Typer CLI, talks to backend via HTTP)
tests/        → integration tests
```

## Build & Development Commands

```bash
# Install all workspace members
uv sync

# Run the CLI
uv run --project packages/cli auralake --help

# Run the backend server
uv run --project packages/backend auralake-server

# Lint
uv run ruff check packages/
uv run ruff format --check packages/

# Type check
uv run mypy packages/

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/test_foo.py::test_bar -v

# Docker
docker compose up -d          # backend + db
```

## Architecture

### `packages/backend/src/auralake_shared/` (shared library, shipped with backend)

- **`core/`** — Cross-cutting infrastructure: structlog logging (`logging.py`), Rich output formatting for table/JSON/CSV (`output.py`), execution context dataclass (`context.py`), exception hierarchy (`exceptions.py`).
- **`models/`** — Pydantic models for all domain objects. Key model files map to domain areas: `config.py` (app config + thresholds), `billing.py` (cost/TCO records, infra costs, RI/savings plan recs), `compute.py` (cluster info & utilization), `recommendations.py` (recommendations with risk levels & savings estimates), `jobs.py` (job profiles & consolidation groups), `query_plans.py` (Spark plan parsing & anti-pattern detection), `dab.py` (Databricks Asset Bundle config & diffs), `policies.py` (cluster policies & tag violations), `routing.py` (workload portability scoring).
- **`providers/`** — Provider registry (`__init__.py`) and abstract base classes (`base.py`). Concrete providers live in backend.

### `packages/backend/src/auralake_backend/` (`auralake_backend`)

- **`server/`** — FastAPI application with 12 feature modules (cost, clusters, resources, spot, delta, jobs, query, policies, budgets, tags, routing, agent).
- **`analyzers/`** — Cost/resource analysis engines.
- **`actions/`** — Action executors for recommendations.
- **`automation/`** — Approval workflows, audit logging, automation engine.
- **`agent/`** — Metrics collector, scheduler, plan parser.
- **`db/`** — SQLModel models, engine init, repositories.
- **`git_integration/`** — DAB diff rendering, PR builder, repo operations.
- **`providers/`** — Concrete provider implementations: `databricks/`, `snowflake/`, `lake_formation/`. Auto-registered on import.

### `packages/cli/` (`auralake_cli`)

- Typer-based CLI that talks to the backend via HTTP (`client.py`).
- Self-contained Rich rendering (`_rendering.py`) and structlog setup (`_logging.py`) — no `auralake_shared` dependency.
- `db.py` commands require `auralake-backend` (optional dependency via `pip install auralake-cli[db]`).

### Key design patterns

- **ExecutionContext** (`core/context.py`) is the central state object threaded through CLI commands — bundles config, provider, automation level, and runtime flags. No global state.
- **Config** lives in the database via connections. Env var `AURALAKE_DATABASE_URL` configures the DB connection.
- **AutomationLevel** (`recommend` → `dry_run` → `apply` → `auto`) controls how aggressively changes are applied, with safety rails via `AutomationConfig` (protected clusters/jobs, bulk action thresholds, max risk level).
- **Provider registration**: Concrete providers call `register_provider()` from `auralake_shared.providers` on import. Backend's `__init__.py` imports all provider packages to trigger registration.
- **All exceptions** inherit from `AuraLakeError` for single-catch handling.

## Code Style

- Python 3.11+ target (ruff target-version), line length 100
- Ruff lint rules: E, F, I, N, W, UP
- Strict mypy with pydantic plugin
- All models use Pydantic v2 (`BaseModel` with `model_validate`)
- Enums use `StrEnum`
