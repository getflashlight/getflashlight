"""``auralake sample`` — download the FinOps FOCUS sample dataset and seed it.

Pulls the official FOCUS-Sample-Data CSV from GitHub, loads it straight into
BRONZE via the vectorized DuckDB path (:mod:`auralake.lake.seed`), and rebuilds
GOLD — so a fresh install has real numbers for the dashboard in one command, no
connector config or cloud credentials needed. Seeded data lives under the
isolated ``focus_sample`` connector, so re-running only replaces the sample.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer

from auralake.core.logging import get_logger
from auralake.lake import bronze, paths, runlog, seed
from auralake.transform.runner import build_gold

logger = get_logger(__name__)

SAMPLE_CONNECTOR = "focus_sample"
_BASE = (
    "https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/"
    "FOCUS-Sample-Data/main/FOCUS-1.0"
)
SAMPLE_URLS = {
    1000: f"{_BASE}/focus_sample.csv",
    10000: f"{_BASE}/focus_sample_10000.csv",
}


def _download(url: str, dest: Path, *, force: bool) -> None:
    if dest.exists() and not force:
        logger.info("sample_cached", path=str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("sample_downloading", url=url)
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)
    logger.info("sample_downloaded", path=str(dest))


def load_sample(rows: int = 1000, url: str | None = None, force: bool = False) -> None:
    """Download the FOCUS sample (``rows`` = 1000 or 10000, or an explicit ``url``)
    and seed BRONZE + GOLD."""
    download_url = url or SAMPLE_URLS.get(rows)
    if download_url is None:
        raise typer.BadParameter("rows must be 1000 or 10000 (or pass --url)")

    paths.ensure_layout()
    dest = paths.home() / "data" / Path(download_url).name
    _download(download_url, dest, force=force)

    run_id = bronze.new_run_id()
    started = datetime.now(UTC)
    count = seed.seed_from_csv(dest, connector=SAMPLE_CONNECTOR, ingest_run_id=run_id)
    runlog.record_run(
        run_id=run_id,
        connector=SAMPLE_CONNECTOR,
        status="success",
        rows=count,
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    published = build_gold()

    typer.echo(f"\nSeeded {count} sample rows → {published} GOLD views.")
    typer.echo("Next: auralake dashboard serve   # http://127.0.0.1:8501")
