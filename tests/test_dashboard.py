"""Dashboard render smoke test — guards the date-range default crash.

A provider whose data starts mid-month gives daily bounds like [May 31, Jun 24].
The per-page default range opens on ``max(bounds_min, trailing_6_months)`` (see
``chrome.months_back`` + ``views/provider_focus.py::render``) specifically so a
naive "first material month" anchor can never land before the actual data start —
this test exercises the real render path (via NiceGUI's user-simulation harness,
the NiceGUI analogue of Streamlit's old ``AppTest``) to catch a regression there.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

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
    when = datetime(2026, 5, day, tzinfo=UTC)
    return FocusRecord(
        provider_name=ProviderName.AWS,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=when,
        charge_period_end=when,
        billed_cost=Decimal("10"),
        effective_cost=Decimal("10"),
        list_cost=Decimal("12"),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name="AmazonEC2",
        tags={"team": "data"},
        x_compute_class=ComputeClass.NOT_APPLICABLE,
        x_source_connector="t",
    )


def test_provider_page_renders_when_data_starts_midmonth(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    # AWS data only on May 31 → daily bounds collapse to a single mid-month day.
    window = IngestWindow(date(2026, 5, 31), date(2026, 5, 31))
    bronze.write_window("t", window, [_rec(31)], ingest_run_id="r1")
    build_gold()

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        # build_pages() must run *inside* the simulation context — it resets
        # NiceGUI's global page registry first, and registers our @ui.page routes
        # against that fresh registry (passing build_pages as user_simulation's
        # `root=` doesn't work: that's for a single native-app root page, not a
        # function that itself registers N page-decorated routes).
        async with user_simulation() as user:
            build_pages()
            await user.open("/")
            await user.should_see("Cloud spend overview")
            await user.open("/aws")
            await user.should_see("Redshift spend")

    asyncio.run(_check())
