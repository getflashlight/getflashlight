"""Bulk billing connectors must not regress to Python row-by-row ingestion."""

from __future__ import annotations

from flashlight.ingest.base import Connector
from flashlight.ingest.connectors.aws_focus import AwsFocusConnector
from flashlight.ingest.connectors.databricks import DatabricksConnector


def test_bulk_cost_connectors_override_the_row_based_ingest_path() -> None:
    assert AwsFocusConnector.ingest is not Connector.ingest
    assert DatabricksConnector.ingest is not Connector.ingest
