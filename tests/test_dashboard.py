"""Dashboard render smoke test — guards the date-range default crash.

A provider whose data starts mid-month gives daily bounds like [May 31, Jun 24],
but the range default opens on the first *material month* (May 1) — which is BEFORE
min_value. Streamlit rejects an out-of-bounds default, so the page must clamp it.
Rendered via AppTest (executes the script like a real session, unlike a health probe).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from auralake.core.settings import get_settings
from auralake.focus.enums import ChargeCategory, ComputeClass, ProviderName, ServiceCategory
from auralake.focus.model import FocusRecord
from auralake.ingest.base import IngestWindow


@pytest.fixture
def lake_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AURALAKE_HOME", str(tmp_path))
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
    from auralake.lake import bronze
    from auralake.transform.runner import build_gold

    # AWS data only on May 31 → daily bounds collapse to a single mid-month day.
    window = IngestWindow(date(2026, 5, 31), date(2026, 5, 31))
    bronze.write_window("t", window, [_rec(31)], ingest_run_id="r1")
    build_gold()

    from streamlit.testing.v1 import AppTest

    import auralake.dashboard.app as app

    at = AppTest.from_file(app.__file__, default_timeout=30).run()
    assert not at.exception, f"dashboard raised: {at.exception}"
    assert any("AWS" in t.value for t in at.title)
