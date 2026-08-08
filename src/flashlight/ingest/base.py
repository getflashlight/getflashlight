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
from flashlight.lake.ai_usage_schema import AiUsageRecord
from flashlight.lake.compute_instance_schema import ComputeInstanceRecord
from flashlight.lake.driver_health_schema import DriverHealthRecord
from flashlight.lake.redshift_policy_config_schema import RedshiftPolicyConfigRecord
from flashlight.lake.redshift_table_observability_schema import RedshiftTableObservabilityRecord
from flashlight.lake.storage_location_schema import StorageLocationRecord

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
        ``ingest/connectors/aws_focus.py``.

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

    def fetch_policy_config(self, window: IngestWindow) -> Iterator[RedshiftPolicyConfigRecord]:
        """Yield policy-control evidence (default: none), collected only at ingest."""
        return iter(())

    def fetch_redshift_table_observability(
        self, window: IngestWindow
    ) -> Iterator[RedshiftTableObservabilityRecord]:
        """Yield durable daily Redshift table/Spectrum facts (default: none).

        This is separate from efficiency summaries: it retains operational facts at
        their natural daily grain so unused-table and external-table analysis remain
        valid after Redshift system-table history expires.
        """
        return iter(())

    def fetch_ai_usage(self, window: IngestWindow) -> Iterator[AiUsageRecord]:
        """Yield aggregated AI serving-usage records for the window (default: none).

        Optional, non-abstract: a source with no model-serving telemetry emits no token
        rows, and a source whose telemetry tables aren't enabled must emit none rather
        than guessing. Measurement only — token/request volume per served model and
        requester, with no dollar figure (the endpoint's spend stays canonical in the
        FOCUS plane). Best-effort — a failure here must not abort the canonical cost
        ingest (the runner warns and skips).
        """
        return iter(())

    def fetch_storage_locations(self, window: IngestWindow) -> Iterator[StorageLocationRecord]:
        """Yield this platform's cloud object-storage locations (default: none).

        Optional, non-abstract, and metadata only — no cost, no utilization. It answers
        "which buckets back this platform?", which is what lets the *cloud provider's*
        storage bill be attributed to the platform sitting on top of it (Databricks'
        own bill covers DBU compute only — see ``docs/design/backing-storage.md``).

        ``window`` is accepted for hook uniformity and **deliberately ignored**: Unity
        Catalog and its equivalents expose only current state, so a pull can only ever
        produce a present-tense snapshot. It's stamped with the month it ran in, the
        same call ``databricks._fetch_table_inventory`` makes.

        Best-effort — a failure here must not abort the canonical cost ingest (the
        runner warns and skips), and an empty result is treated as "couldn't see
        anything", never as "this platform has no storage" (see
        ``lake.storage_locations.write_storage_locations``).
        """
        return iter(())

    def fetch_compute_instances(self, window: IngestWindow) -> Iterator[ComputeInstanceRecord]:
        """Yield this platform's cloud-compute-instance membership map (default: none).

        Optional, non-abstract, and metadata only — no cost, no utilization. It answers
        "which cloud VM backed this cluster/job?", which is what lets the *cloud
        provider's* compute bill be attributed to the platform sitting on top of it
        (Databricks' own bill covers DBU compute only — see
        ``docs/design/backing-compute.md``).

        Unlike :meth:`fetch_storage_locations`, ``window`` is genuinely honored here:
        the source (e.g. Databricks' ``system.compute.node_timeline``) reports real
        historical instance activity bounded by time, not a present-tense snapshot, so
        a pull for a given window is authoritative for that window and the writer does
        a real partition-replace (see ``lake.compute_instances.write_compute_instances``).

        Best-effort — a failure here must not abort the canonical cost ingest (the
        runner warns and skips).
        """
        return iter(())
