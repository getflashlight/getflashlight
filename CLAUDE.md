# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Auralake

Auralake is a multi-platform lakehouse cost optimization CLI tool. It analyzes Databricks (and potentially other lakehouse providers) usage, billing, compute, and job data to generate cost-saving recommendations. It can apply changes via Databricks Asset Bundle (DAB) modifications and submit them as GitHub pull requests.

## Build & Development Commands

```bash
# Install dependencies (uses uv with Python 3.14)
uv sync

# Install with dev dependencies
uv sync --extra dev

# Run the CLI
uv run auralake
# or: python -m auralake

# Lint
uv run ruff check src/
uv run ruff format --check src/

# Type check
uv run mypy src/

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/test_foo.py::test_bar -v
```

## Architecture

### Source layout (`src/auralake/`)

- **`core/`** — Cross-cutting infrastructure: YAML config loading with env-var overrides (`config.py`), structlog logging (`logging.py`), Rich output formatting for table/JSON/CSV (`output.py`), execution context dataclass (`context.py`), exception hierarchy (`exceptions.py`).
- **`models/`** — Pydantic models for all domain objects. Key model files map to domain areas: `config.py` (app config + thresholds), `billing.py` (cost/TCO records, infra costs, RI/savings plan recs), `compute.py` (cluster info & utilization), `recommendations.py` (recommendations with risk levels & savings estimates), `jobs.py` (job profiles & consolidation groups), `query_plans.py` (Spark plan parsing & anti-pattern detection), `dab.py` (Databricks Asset Bundle config & diffs), `policies.py` (cluster policies & tag violations), `routing.py` (workload portability scoring).
- **`cli/`** — Typer-based CLI entry point (referenced at `auralake.cli.main:app` but not yet implemented).
- **`providers/`** — Provider abstraction layer (referenced by `core/context.py` via `AbstractProvider` but not yet implemented). Currently Databricks-focused with planned multi-provider support.

### Key design patterns

- **ExecutionContext** (`core/context.py`) is the central state object threaded through CLI commands — bundles config, provider, automation level, and runtime flags. No global state.
- **Config resolution** follows a priority chain: explicit path → `AURALAKE_CONFIG` env var → `auralake.yaml` in CWD → `~/.auralake/config.yaml`. Env vars `AURALAKE_DATABASE_URL` and `AURALAKE_PROVIDER` override specific fields.
- **AutomationLevel** (`recommend` → `dry_run` → `apply` → `auto`) controls how aggressively changes are applied, with safety rails via `AutomationConfig` (protected clusters/jobs, bulk action thresholds, max risk level).
- **All exceptions** inherit from `AuraLakeError` for single-catch handling.

## Code Style

- Python 3.11+ target (ruff target-version), line length 100
- Ruff lint rules: E, F, I, N, W, UP
- Strict mypy with pydantic plugin
- All models use Pydantic v2 (`BaseModel` with `model_validate`)
- Enums use `StrEnum`
