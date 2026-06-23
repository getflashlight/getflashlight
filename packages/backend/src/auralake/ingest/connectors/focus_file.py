"""Local FOCUS file connector — ingest a FOCUS CSV/Parquet from disk.

The simplest way to load real FOCUS sample data (e.g. the FinOps Foundation
FOCUS-Sample-Data sets) or any vendor's FOCUS export without cloud credentials.
Reuses the shared FOCUS mapper, so it behaves identically to ``aws_focus``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import pyarrow.parquet as pq

from auralake.core.exceptions import ConnectorError
from auralake.core.logging import get_logger
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import FocusFileConfig
from auralake.ingest.connectors._focus_map import map_focus_row

logger = get_logger(__name__)


class FocusFileConnector(Connector):
    name = "focus_file"

    def __init__(self, config: FocusFileConfig) -> None:
        self._config = config

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        path = Path(self._config.path)
        if not path.exists():
            raise ConnectorError(self.name, f"File not found: {path}")

        rows = self._read_parquet(path) if _is_parquet(path) else self._read_csv(path)
        kept = 0
        for row in rows:
            record = map_focus_row(row, self.name)
            if record is None:
                continue
            if self._config.respect_window and not _in_window(record, window):
                continue
            kept += 1
            yield record
        logger.info("focus_file_read", path=str(path), rows=kept)

    def _read_csv(self, path: Path) -> Iterator[dict[str, object]]:
        with path.open(newline="") as f:
            yield from csv.DictReader(f)

    def _read_parquet(self, path: Path) -> Iterator[dict[str, object]]:
        table = pq.read_table(path)  # type: ignore[no-untyped-call]
        yield from table.to_pylist()


def _is_parquet(path: Path) -> bool:
    return path.suffix.lower() in {".parquet", ".pq"}


def _in_window(record: FocusRecord, window: IngestWindow) -> bool:
    return (window.start <= record.billing_period_start <= window.end) or (
        window.start <= record.charge_period_start.date() <= window.end
    )
