# Contributing

## Setup

```bash
uv sync
uv run flashlight init             # scaffold the lake home + connections.yml
uv run flashlight sample           # download the FinOps FOCUS sample + seed it
```

See `CLAUDE.md` for an architecture overview.

## Before opening a PR

Run the same gate CI runs:

```bash
uv run ruff check src tests
uv run mypy src tests
uv run pytest
```

`pre-commit` is configured too — `uv run pre-commit install` to run it on every commit.

## Submitting

`main` is protected: a PR is required, and it must pass CI (lint, type-check, test on
Python 3.12/3.13/3.14) before it can merge. No approval is required to merge your own
PR once checks are green.

Keep PRs focused — one change, one PR. Explain the *why* in the description; the diff
already shows the *what*.
