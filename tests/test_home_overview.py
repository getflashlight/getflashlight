"""Homepage figures must reconcile with the Efficiency & Waste action queue."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from flashlight.dashboard.views import home_overview


def test_recoverable_by_provider_uses_action_queue_rollup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Two findings on one job count as one best action, as they do in the drill-down."""
    rows = pd.DataFrame(
        [
            {
                "provider_name": "Databricks",
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "WASTE",
                "recoverable_cost": 100.0,
                "billed_cost": 200.0,
                "confidence": "high",
            },
            {
                "provider_name": "Databricks",
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "WASTE",
                "recoverable_cost": 60.0,
                "billed_cost": 200.0,
                "confidence": "candidate",
            },
            {
                "provider_name": "AWS",
                "entity_id": "bucket-1",
                "entity_type": "storage",
                "lens": "WASTE",
                "waste_category": "s3_old_object",
                "recoverable_cost": 999.0,
                "billed_cost": 1_000.0,
                "confidence": "high",
            },
            {
                "provider_name": "AWS",
                "entity_id": "warehouse-1",
                "entity_type": "sql_warehouse",
                "lens": "WASTE",
                "waste_category": "sql_warehouse_low_cache_reuse",
                "recoverable_cost": 50.0,
                "billed_cost": 100.0,
                "confidence": "candidate",
            },
            {
                "provider_name": "Databricks",
                "entity_id": "cluster-1",
                "entity_type": "interactive",
                "lens": "OPPORTUNITY",
                "recoverable_cost": 25.0,
                "billed_cost": 200.0,
                "confidence": "candidate",
            },
        ]
    )
    monkeypatch.setattr(home_overview, "gold_df", lambda _sql: rows)

    recoverable = home_overview._recoverable_by_provider(date(2026, 6, 1))  # noqa: SLF001

    assert recoverable["Databricks"] == pytest.approx(125.0)
    assert recoverable["AWS"] == pytest.approx(50.0)
