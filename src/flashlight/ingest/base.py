"""Connector contract. Every source maps its native billing into FOCUS rows."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date

from flashlight.core.exceptions import FocusValidationError
from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord
from flashlight.focus.model import FocusRecord
from flashlight.lake.driver_health_schema import DriverHealthRecord

# A progress event: (event, connector_name, rows). "start" (rows always 0),
# "rows" (running count, emitted every PROGRESS_EVERY rows — row-based
# connectors only; a vectorized connector's single COPY has no per-row hook to
# tick from), "done" (final row count), "failed" (rows always 0).
ProgressCallback = Callable[[str, str, int], None]
PROGRESS_EVERY = 10_000


@dataclass(frozen=True)
class IngestWindow:
    """Inclusive date range to ingest [start, end]."""

    start: date
    end: date


class Connector(ABC):
    """A billing source. Implementations are stateless per ``fetch``/``ingest`` call."""

    #: Stable identifier, stamped onto every row as ``x_source_connector``.
    name: str = "base"

    #: Set by a connector whose fetch() intentionally yields nothing (cost pulled by
    #: a different connector instead) — surfaced on ingest_ok's log line so
    #: ``rows=0`` there reads as "by design", not "the pull came back empty".
    cost_pull_note: str | None = None

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        """Yield canonical FOCUS records for the window, one Python object per row.

        Implement this for a connector whose source must be turned into
        FocusRecord objects in Python — an API/SDK pull that returns discrete
        rows (Databricks SQL results, Cost Explorer group-bys, Redshift
        telemetry). The default :meth:`ingest` drains this into BRONZE, chunked
        so memory stays bounded regardless of pull size.

        A connector whose source is already FOCUS-shaped and DuckDB-scannable
        (a FOCUS Parquet/CSV file, local or in a cloud Data Export) should
        instead override :meth:`ingest` directly with one vectorized DuckDB SQL
        write (see :mod:`flashlight.focus.sql_mapping`) and leave this method
        unimplemented — no FocusRecord objects, no per-row Python at all. See
        ``ingest/connectors/aws_focus.py`` / ``focus_file.py``.

        Implementations must stamp ``x_source_connector = self.name`` on each
        record and pick FOCUS-valid controlled-vocabulary values, falling back
        to ``OTHER`` rather than inventing new ones.
        """
        raise NotImplementedError(f"{type(self).__name__} must override fetch() or ingest()")
        yield  # pragma: no cover - marks this a generator if ever reached

    def ingest(
        self,
        window: IngestWindow,
        *,
        run_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Pull this connector's data for ``window`` straight into BRONZE. Returns rows.

        Default: drains :meth:`fetch` through a currency assertion into
        :func:`flashlight.lake.bronze.write_window`, which itself chunks the
        write so memory stays bounded regardless of pull size. Override this
        (and leave :meth:`fetch` unimplemented) for a source DuckDB can read
        and map directly — see :meth:`fetch`'s docstring.
        """
        from flashlight.lake import bronze  # lazy: bronze imports IngestWindow from here

        base_currency = get_settings().base_currency

        def _checked() -> Iterator[FocusRecord]:
            count = 0
            for record in self.fetch(window):
                if record.billing_currency != base_currency:
                    raise FocusValidationError(
                        f"{self.name}: currency {record.billing_currency} "
                        f"!= base {base_currency}; mixed-currency sums are unsafe"
                    )
                count += 1
                if on_progress and count % PROGRESS_EVERY == 0:
                    on_progress("rows", self.name, count)
                yield record

        return bronze.write_window(self.name, window, _checked(), ingest_run_id=run_id)

    def fetch_efficiency(self, window: IngestWindow) -> Iterator[EfficiencyRecord]:
        """Yield aggregated efficiency records for the window (default: none).

        Optional, non-abstract: a source without utilization/activity telemetry emits
        no waste rows. Connectors that have it (e.g. Databricks system tables) override
        this to power the efficiency/waste GOLD view. Best-effort — a failure here must
        not abort the canonical cost ingest (the runner warns and skips).
        """
        return iter(())

    def fetch_driver_health(self, window: IngestWindow) -> Iterator[DriverHealthRecord]:
        """Yield aggregated client-driver records for the window (default: none).

        Optional, non-abstract, and independent of :meth:`fetch_efficiency` — this is a
        fleet-health/compliance signal (which JDBC/ODBC driver versions are in use), not
        a waste signal. Best-effort — a failure here must not abort the canonical cost
        ingest (the runner warns and skips).
        """
        return iter(())
