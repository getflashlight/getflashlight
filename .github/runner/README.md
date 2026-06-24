# Docker-based self-hosted GitHub Actions runner

A containerized Actions runner for Auralake CI/CD, with `uv` + `git` preinstalled.
This is **CI infrastructure** — the app itself ships as a pip wheel with no Docker.

## Run it

```bash
cd .github/runner
cp .env.example .env     # then edit (see below)
docker compose up -d --build
```

Provide **one** credential in `.env`:

- `GITHUB_PAT` — a fine-grained PAT with **Administration: read/write** on the repo.
  The container mints a fresh registration token on every (re)start. Recommended.
- `RUNNER_TOKEN` — a short-lived token from **Repo → Settings → Actions → Runners →
  New self-hosted runner** (expires in ~1h; fine for a quick test).

| Env var | Default | Purpose |
|---|---|---|
| `GITHUB_URL` | `https://github.com/ychaparala/auralake` | repo the runner joins |
| `RUNNER_NAME` | `auralake-docker` | runner name shown in GitHub |
| `RUNNER_LABELS` | `self-hosted,docker,linux,x64` | targeting labels |
| `RUNNER_EPHEMERAL` | `false` | `true` = exit after one job (for autoscaling) |

Scale to N parallel runners:

```bash
docker compose up -d --build --scale runner=3
```

The runner **deregisters itself** on `docker stop` (SIGTERM → `config.sh remove`).

## Use it from a workflow

Point a job at the runner's labels instead of `ubuntu-latest`:

```yaml
jobs:
  test:
    runs-on: [self-hosted, docker]
```

The `ci.yml` / `release.yml` workflows ship with `runs-on: ubuntu-latest`
(GitHub-hosted). Switch them only if you want them on your own runner.

## Notes

- The image pulls the **latest** runner release at build time. Rebuild
  (`--build`) periodically; GitHub deprecates old runner versions.
- Jobs run `uv sync --locked` + ruff + mypy + pytest — `uv` provides the Python
  toolchain, so no separate Python install is needed in the image.
