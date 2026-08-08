"""Request-scoped dashboard query cache behavior."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.focus.enums import ChargeCategory, ComputeClass, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow


@pytest.fixture
def lake_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(day: int) -> FocusRecord:
    return FocusRecord(
        billing_account_id="a",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_category=ChargeCategory.USAGE,
        charge_period_start=datetime(2026, 5, day, tzinfo=UTC),
        charge_period_end=datetime(2026, 5, day, tzinfo=UTC),
        billed_cost=Decimal("15"),
        effective_cost=Decimal("15"),
        list_cost=Decimal("15"),
        provider_name=ProviderName.AWS,
        service_name="Amazon Redshift",
        service_category=ServiceCategory.COMPUTE,
        x_compute_class=ComputeClass.NOT_APPLICABLE,
        x_source_connector="t",
    )


def test_gold_session_caches_identical_sql_but_returns_independent_frames(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard import data
    from flashlight.lake import bronze, duck
    from flashlight.transform.runner import build_gold
    bronze.write_window(
        "t", IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), [_rec(15)], ingest_run_id="r1"
    )
    build_gold()

    real_connect = duck.connect
    selects: list[str] = []

    class _Connection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def execute(self, sql: str) -> Any:
            if sql.strip().upper().startswith("SELECT"):
                selects.append(sql)
            return self._connection.execute(sql)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

    def counting_connect() -> Any:
        return _Connection(real_connect())

    with __import__("pytest").MonkeyPatch.context() as mp:
        mp.setattr(duck, "connect", counting_connect)
        with data.gold_session():
            first = data.gold_df('SELECT * FROM "aws".monthly_bill')
            first["mutated"] = True
            second = data.gold_df('SELECT * FROM "aws".monthly_bill')

    assert len(selects) == 1
    assert "mutated" not in second.columns
