"""Local FOCUS file connector — ingest a FOCUS CSV/Parquet from disk.

The simplest way to load real FOCUS sample data (e.g. the FinOps Foundation
FOCUS-Sample-Data sets) or any vendor's FOCUS export without cloud credentials.
Reads and maps entirely in DuckDB (:mod:`flashlight.focus.sql_mapping`) — no
FocusRecord objects, no per-row Python — the same mapping rules ``aws_focus``
uses, so a local file and an AWS Data Export map identically.
"""

from __future__ import annotations

from pathlib import Path

from flashlight.core.exceptions import ConnectorError
from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.focus import sql_mapping
from flashlight.ingest.base import Connector, IngestWindow, ProgressCallback
from flashlight.ingest.config import FocusFileConfig
from flashlight.lake import bronze, duck

logger = get_logger(__name__)


class FocusFileConnector(Connector):
    name = "focus_file"

    def __init__(self, config: FocusFileConfig) -> None:
        self._config = config

    def ingest(
        self,
        window: IngestWindow,
        *,
        run_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        path = Path(self._config.path)
        if not path.exists():
            raise ConnectorError(self.name, f"File not found: {path}")

        source_sql = _read_expr(path)
        con = duck.connect()
        try:
            sql_mapping.ensure_helpers(con)
            present = sql_mapping.present_columns(con, source_sql)
            mapped = sql_mapping.mapping_sql(
                source_sql, connector=self.name, run_id=run_id, present=present
            )
            if self._config.respect_window:
                mapped = f"SELECT * FROM ({mapped}) WHERE {_window_predicate(window)}"
            written = bronze.write_window_sql(
                self.name,
                window,
                con,
                mapped,
                base_currency=get_settings().base_currency,
            )
        finally:
            con.close()
        logger.info("focus_file_read", path=str(path), rows=written)
        return written


def _is_parquet(path: Path) -> bool:
    return path.suffix.lower() in {".parquet", ".pq"}


def _read_expr(path: Path) -> str:
    quoted = str(path).replace("'", "''")
    if _is_parquet(path):
        return f"read_parquet('{quoted}')"
    return f"read_csv('{quoted}', header=true, all_varchar=true)"


def _window_predicate(window: IngestWindow) -> str:
    """Same overlap rule the old Python ``_in_window`` used: keep a row if either
    its billing period or its charge period falls in ``[window.start, window.end]``.
    """
    start, end = window.start.isoformat(), window.end.isoformat()
    return (
        f"(billing_period_start BETWEEN DATE '{start}' AND DATE '{end}') "
        f"OR (CAST(charge_period_start AS DATE) BETWEEN DATE '{start}' AND DATE '{end}')"
    )
