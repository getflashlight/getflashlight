"""Placeholder connectors for sources beyond the v1 (Databricks + AWS) scope.

These are wired into the registry so config/validation paths exist, but raise on
``fetch``. Each notes the native-export path we'll lean on when implemented.
"""

from __future__ import annotations

from collections.abc import Iterator

from flashlight.core.exceptions import ConnectorError
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import Connector, IngestWindow


class _NotYet(Connector):
    name = "stub"
    native_export: str = ""

    def __init__(self, *_: object, **__: object) -> None:  # noqa: D401 - stub
        pass

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        raise ConnectorError(
            self.name,
            f"Connector not implemented in v1. Planned path: {self.native_export}",
        )
        yield  # pragma: no cover - marks this a generator


class BigQueryConnector(_NotYet):
    name = "bigquery"
    native_export = "GCP FOCUS-aligned Cloud Billing export to BigQuery"


class SnowflakeConnector(_NotYet):
    name = "snowflake"
    native_export = "Snowflake ORGANIZATION_USAGE views → custom FOCUS mapper"
