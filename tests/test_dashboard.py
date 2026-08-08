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
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.focus.enums import (
    ChargeCategory,
    CommitmentDiscountCategory,
    CommitmentDiscountStatus,
    ComputeClass,
    ProviderName,
    ServiceCategory,
)
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
    # Redshift's own service name, because /aws is scoped to it: with out-of-scope spend
    # the page correctly renders an empty state, which exercises none of the date-bounds
    # arithmetic this test exists for.
    window = IngestWindow(date(2026, 5, 31), date(2026, 5, 31))
    bronze.write_window("t", window, [_redshift_usage(31, "10")], ingest_run_id="r1")
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
            await user.should_see("Data Cloud Spend overview")
            await user.open("/aws")
            await user.should_see("AWS Redshift spend")

    asyncio.run(_check())


def test_provider_page_reachable_after_first_sync_post_boot(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Regression: a provider's GOLD group can appear *after* the dashboard has
    already booted (its first successful sync, run from the Connections page in
    the same long-running process). discover_provider_groups() reads gold/ live
    so the nav link shows up immediately — the page route must be reachable too,
    not 404 until the process is restarted (router.py used to register one
    @ui.page per group discovered only at build_pages() time).
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            # No GOLD published yet at boot — discover_provider_groups() is empty.
            build_pages()

            # A sync completes *after* boot, publishing the "aws" group for the
            # first time — same timing as a user adding a connection and hitting
            # Sync in the Connections page of an already-running dashboard.
            from flashlight.lake import bronze
            from flashlight.transform.runner import build_gold

            window = IngestWindow(date(2026, 5, 31), date(2026, 5, 31))
            bronze.write_window("t", window, [_redshift_usage(31, "10")], ingest_run_id="r1")
            build_gold()

            await user.open("/aws")
            await user.should_see("AWS Redshift spend")

    asyncio.run(_check())


def test_provider_page_renders_commitment_panel_when_present(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The commitment-coverage panel (added alongside FOCUS Contract Commitment
    support) must render real Used/Unused data when present, and the existing
    smoke test above already proves it renders nothing (no crash) when absent."""
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    # AWS's own page is entirely rendered by redshift_focus.render() (see
    # router.py's `group == "aws"` branch) — its bounds check scopes to Redshift's
    # own FOCUS service names, so the record needs one for the page to render past
    # that check into the Breakdown tab where the commitment panel lives.
    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    used = _rec(15)
    used.service_name = "Amazon Redshift"
    used.commitment_discount_id = "cud-1"
    used.commitment_discount_type = "Savings Plan"
    used.commitment_discount_category = CommitmentDiscountCategory.SPEND
    used.commitment_discount_status = CommitmentDiscountStatus.USED
    unused = _rec(16)
    unused.service_name = "Amazon Redshift"
    unused.commitment_discount_id = "cud-2"
    unused.commitment_discount_type = "Savings Plan"
    unused.commitment_discount_category = CommitmentDiscountCategory.SPEND
    unused.commitment_discount_status = CommitmentDiscountStatus.UNUSED
    bronze.write_window("t", window, [used, unused], ingest_run_id="r1")
    build_gold()

    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            user.find(kind=ui.tab, content="Breakdown").click()
            await user.should_see("Commitment coverage")

    asyncio.run(_check())


def _credit_rec(day: int, amount: str) -> FocusRecord:
    """A one-off credit line, shaped like the real AWS one that motivated this: a
    goodwill credit applied against Redshift in a single month, its identity carried in
    ChargeDescription (name + credit id) and its cost negative."""
    rec = _rec(day)
    rec.service_name = "Amazon Redshift"
    rec.charge_category = ChargeCategory.CREDIT
    rec.charge_description = "Goodwill Credits, credit id: 10063543426"
    rec.effective_cost = Decimal(amount)
    rec.billed_cost = Decimal(amount)
    rec.list_cost = Decimal(amount)
    return rec


def _redshift_usage(day: int, amount: str) -> FocusRecord:
    rec = _rec(day)
    rec.service_name = "Amazon Redshift"
    rec.effective_cost = Decimal(amount)
    rec.billed_cost = Decimal(amount)
    rec.list_cost = Decimal(amount)
    return rec


def test_home_headline_excludes_one_off_credits(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A one-off credit must not read as a spend collapse on the home page.

    Real case: a −$46K AWS Redshift goodwill credit landed in one month and the
    net-based headline reported the month as a −$46K *drop* in spend, with the credit's
    own service topping "Biggest movers". The KPIs, trend, share and movers are all
    charges-only now (monthly_bill.gross_cost); the note under the KPI row says so, and
    the credit's line items are on the provider's page (gold.credits_month).
    """
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window(
        "t",
        window,
        [_redshift_usage(15, "1000"), _credit_rec(16, "-900")],
        ingest_run_id="r1",
    )
    build_gold()

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/")
            # Charges only: $1,000, not the $100 net the credit would leave behind.
            await user.should_see("$1K")
            await user.should_not_see("$100")
            # …and the page says so rather than presenting gross as the whole bill.
            await user.should_see("Charges only — credits excluded.")

    asyncio.run(_check())


def test_home_page_date_bounds_union_across_every_provider(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The home page's default range (its YTD anchor included) must reflect the UNION
    of every provider's own span, not just one of them.

    Regression: ``discover_provider_groups()`` sorts by name ('aws' < 'gcp'), and the
    old code kept whichever group's ``(min charge_day, max charge_day)`` it saw *first*
    and silently ignored every other group's — so a later-sorted provider with an
    earlier start date (or a later end date) never widened ``bounds_min``/``bounds_max``
    at all. Here AWS (May) sorts first but GCP (March) has the true earlier bound; the
    default start must be GCP's, not AWS's own.
    """
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    aws = _rec(20)  # AWS, May 2026 — the alphabetically-first group
    gcp = _rec(1)
    gcp.provider_name = ProviderName.GCP
    gcp.billing_period_start = date(2026, 3, 1)
    gcp.billing_period_end = date(2026, 3, 31)
    gcp.charge_period_start = datetime(2026, 3, 1, tzinfo=UTC)
    gcp.charge_period_end = datetime(2026, 3, 1, tzinfo=UTC)

    bronze.write_window(
        "t", IngestWindow(date(2026, 3, 1), date(2026, 5, 31)), [aws, gcp], ingest_run_id="r1"
    )
    build_gold()

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/")
            # Fixed: the default start is GCP's Mar 1 (the true union minimum). The bug
            # would show "May 20" here — AWS's own span, with GCP's dropped entirely.
            await user.should_see("Mar 1")

    asyncio.run(_check())


def test_home_and_nav_label_aws_group_as_aws_redshift(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The AWS group is labelled for what it holds. ``aws_focus`` ingests a
    service-scoped slice of the AWS bill (Redshift by default) and /aws is a
    Redshift-only page, so a bare "AWS" overstates it — data.provider_label maps the
    group to "AWS Redshift" everywhere a human reads it (nav, home cards, page title),
    while the rows themselves still say provider_name='AWS'.
    """
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_redshift_usage(15, "1000")], ingest_run_id="r1")
    build_gold()

    from flashlight.dashboard.data import provider_label, provider_name_for_group

    assert provider_label("aws") == "AWS Redshift"
    assert provider_name_for_group("aws") == "AWS"  # the filterable value is untouched

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/")
            await user.should_see("AWS Redshift")
            await user.open("/aws")
            await user.should_see("AWS Redshift spend")

    asyncio.run(_check())


def test_aws_label_stays_redshift_when_only_s3_is_extra_in_bronze(  # type: ignore[no-untyped-def]
    lake_home,
) -> None:
    """S3 in bronze must not widen the AWS nav label.

    ``include_services`` still pulls S3 for the storage plane, but ``aws.*`` GOLD
    excludes it (``silver.focus_provider_bill``), so the human label stays
    ``AWS Redshift`` when Redshift is the only service left in that group.
    ``provider_name`` is untouched either way: the label is display-only.
    """
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    s3 = _rec(16)
    s3.service_name = "Amazon Simple Storage Service"
    s3.resource_id = "arn:aws:s3:::acme-lakehouse"
    bronze.write_window("t", window, [_redshift_usage(15, "1000"), s3], ingest_run_id="r1")
    build_gold()

    from flashlight.dashboard.data import provider_label, provider_name_for_group

    assert provider_label("aws") == "AWS Redshift"
    assert provider_name_for_group("aws") == "AWS"


def test_redshift_page_itemizes_credits(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The credit the home page leaves out of its headline is itemized here — by credit
    line, with the amount, so "why did July drop?" has an answer on the page."""
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window(
        "t",
        window,
        [_redshift_usage(15, "1000"), _credit_rec(16, "-900")],
        ingest_run_id="r1",
    )
    build_gold()

    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("Credits & Discounts")  # KPI card
            user.find(kind=ui.tab, content="Breakdown").click()
            await user.should_see("Discounts & credits")  # the table's own panel
            # ui.table rows are data, not text nodes should_see can match.
            rows = " ".join(str(t.rows) for t in user.find(kind=ui.table).elements)
            assert "Goodwill Credits, credit id: 10063543426" in rows
            assert "$-900" in rows

    asyncio.run(_check())


def test_icon_routes_answer_the_probes_browsers_make(lake_home) -> None:  # type: ignore[no-untyped-def]
    """/favicon.ico and Safari's apple-touch-icon probes must be served, not fall
    through to the "/{group}" catch-all page — each miss logged a WARNING per request
    ("…/apple-touch-icon.png not found") and answered an image request with an HTML
    404 page.
    """
    from flashlight.dashboard import router

    router.register_icon_routes()
    router.register_icon_routes()  # idempotent: a second call must not double-register

    from nicegui import app

    ours = [r for r in app.routes if getattr(r, "endpoint", None) is router.icon_response]
    paths = sorted(str(getattr(r, "path", "")) for r in ours)
    assert paths == sorted(router._ICON_ROUTES)  # one route each, no duplicates

    response = router.icon_response()
    assert response.media_type == "image/svg+xml"
    assert b"<svg" in response.body


def test_retired_page_urls_redirect_instead_of_404ing(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A deleted page's URL outlives the page in open tabs, bookmarks and history.

    /tco (removed with the silver.tco_* views and the gold/shared group), /utilization and
    /leaderboard (now the Efficiency & Waste and Attribution tabs on each provider page)
    all fell through to the "/{group}" catch-all, which answered an HTML 404 and logged
    "http://…/tco not found" once per request. Each must redirect instead — and be
    registered *ahead* of the catch-all, since Starlette matches in registration order.

    Loops over the map rather than naming one path: the next retirement should be covered
    by adding a dict entry, not by remembering to extend this test.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard import router

    assert router._RETIRED_ROUTES, "the map is the test's subject — an empty one proves nothing"

    async def _check() -> None:
        async with user_simulation() as user:  # noqa: F841 - resets the page registry
            router.build_pages()

            from nicegui import app

            paths = [str(getattr(route, "path", "")) for route in app.routes]
            for path, target in router._RETIRED_ROUTES.items():
                assert paths.count(path) == 1, f"{path}: registered once, not per build_pages"
                assert paths.index(path) < paths.index("/{group}"), f"{path}: after catch-all"

                endpoint = next(
                    route.endpoint  # type: ignore[attr-defined]
                    for route in app.routes
                    if str(getattr(route, "path", "")) == path
                )
                redirect = endpoint()
                assert redirect.status_code == 307  # temporary: never browser-cached
                assert redirect.headers["location"] == target

    asyncio.run(_check())


def test_provider_page_renders_when_lake_predates_a_catalogued_view(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Regression: a GOLD lake published before a view was added must not 500 the page.

    ``duck.register_gold`` only registers files that exist, so a view added to the
    catalog after the last ``flashlight transform`` is absent from the DuckDB catalog
    entirely and querying it raises — which took down the whole provider route when
    ``spend_by_service_month``/``spend_forecast_month`` shipped. The panels reading
    them now check ``gold_view_published`` first (see provider_focus.py,
    attribution.py).
    """
    from flashlight.lake import bronze, paths
    from flashlight.transform.runner import build_gold

    # GCP routes to the plain provider_focus page (AWS → redshift_focus, Databricks →
    # the extra-tabs variant), which is where both affected panels live.
    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    rec = _rec(15)
    rec.provider_name = ProviderName.GCP
    bronze.write_window("t", window, [rec], ingest_run_id="r1")
    build_gold()

    # Roll the published lake back to what an older transform would have left behind.
    for view in ("spend_by_service_month", "spend_forecast_month"):
        (paths.gold_dir() / "gcp" / f"{view}.parquet").unlink()

    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/gcp")
            await user.should_see("GCP spend")
            user.find(kind=ui.tab, content="Attribution").click()
            await user.should_see("Spend by service isn't published yet")
            await user.should_see("run `flashlight transform`")

    asyncio.run(_check())


def test_connections_page_renders_sync_toolbar_and_empty_states(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Regression smoke test for the Connections page toolbar (the shared
    chrome.date_range_control popover, not a bespoke one-off — see
    connections.py) and its empty states, with no data sources configured.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/connections")
            await user.should_see("Connections")
            await user.should_see("Full refresh")
            await user.should_see("No data sources yet")
            await user.should_see("No syncs yet")

            # The date-range trigger is the same chrome.date_range_control
            # popover used on every other page (its own click-to-expand
            # behavior is covered where it's defined) — just confirm
            # connections.py actually wired one up.
            assert "icon=event" in str(user.current_layout)

    asyncio.run(_check())


def test_connections_page_renders_multiple_sync_history_groups(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Regression test: history_body() reused the name `detail` for both the
    whole per-connector-rows DataFrame (read_runs()'s result) and, inside the
    nested per-connector loop, each row's own error-detail string — the second
    assignment clobbered the DataFrame variable, so the SECOND run group's
    `connectors_df = detail[detail["run_id"] == run_id]` line crashed with
    "'NoneType' object is not subscriptable" (detail was still a plain string/
    None left over from the first group's inner loop). Only reproduces with 2+
    run groups — a single-sync test wouldn't hit the second outer-loop
    iteration where the clobbered variable actually gets read.
    """
    from datetime import UTC, datetime

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake import runlog

    def _ts(minute: int) -> datetime:
        return datetime(2026, 1, 1, 0, minute, tzinfo=UTC)

    runlog.record_run(
        run_id="sync-1",
        connector="aws_focus",
        status="success",
        rows=10,
        started_at=_ts(0),
        finished_at=_ts(1),
    )
    runlog.record_run(
        run_id="sync-2",
        connector="databricks",
        status="failed",
        rows=0,
        started_at=_ts(2),
        finished_at=_ts(3),
        detail="expired token",
    )

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/connections")
            await user.should_see("Connections")
            await user.should_see("aws_focus")
            await user.should_see("databricks")
            await user.should_see("expired token")

    asyncio.run(_check())


def test_connections_page_sync_button_streams_output_without_crashing(  # type: ignore[no-untyped-def]
    lake_home, monkeypatch
) -> None:
    """End-to-end regression for the "Sync now" click path.

    Two real bugs happened here and never showed up in a plain render() smoke
    test, because both needed an actual button click to trigger: (1)
    ui.timer(0.1, ..., once=True) firing after the page's slot was torn down
    ("parent slot ... has been deleted"), and (2) its replacement,
    background_tasks.create(), running with no slot context at all ("the
    current slot cannot be determined") — which additionally blocked
    render()'s own remaining code (including wiring up this very button) when
    awaited inline instead. asyncio.create_subprocess_exec is mocked so this
    doesn't need a real `flashlight ingest` subprocess; nicegui's own
    core.app.handle_exception is wrapped to fail the test loudly if anything
    the button click triggers raises, instead of silently swallowing it the
    way the app itself does in production.
    """
    from nicegui import core, ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    class _FakeStdout:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = lines

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self) -> bytes:
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

    class _FakeProcess:
        def __init__(self, lines: list[bytes], returncode: int) -> None:
            self.stdout = _FakeStdout(lines)
            self._returncode = returncode

        async def wait(self) -> int:
            return self._returncode

    async def _fake_create_subprocess_exec(*cmd, **kwargs):  # type: ignore[no-untyped-def]
        lines = [b"  Prod-Focus ...\n", b"  Prod-Focus ... 42 rows done\n"]
        return _FakeProcess(lines, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    caught: list[Exception] = []
    orig_handle_exception = core.app.handle_exception

    def _loud_handle_exception(exception: Exception) -> None:
        caught.append(exception)
        return orig_handle_exception(exception)

    monkeypatch.setattr(core.app, "handle_exception", _loud_handle_exception)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/connections")
            await user.should_see("Full refresh")

            user.find("Sync now").click()
            await user.should_see("Syncing all connections")

            for _ in range(20):
                await asyncio.sleep(0.05)
                try:
                    await user.should_see("exit code")
                    break
                except AssertionError:
                    continue
            await user.should_see("exit code")

            log_lines = [
                child.text
                for element in user.find(kind=ui.log).elements
                for child in element
                if isinstance(child, ui.label)
            ]
            assert "  Prod-Focus ..." in log_lines
            assert "  Prod-Focus ... 42 rows done" in log_lines

    asyncio.run(_check())
    assert not caught, f"sync click triggered unexpected exception(s): {caught}"


def test_assistant_page_sends_a_question_and_renders_the_reply(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """BYOK assistant page: typing a question and clicking Send wires through to the
    engine and renders the reply — without ever calling a real LLM provider."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.assistant_engine import AssistantTurnResult
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import assistant as assistant_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        messages.append({"role": "assistant", "content": "The answer is 42."})
        return AssistantTurnResult(text="The answer is 42.", steps=[])

    monkeypatch.setattr(assistant_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            # "Assistant" (the nav link) renders synchronously before the async page body
            # resumes past `await client.connected()` — waiting on it is not proof the
            # settings fields exist yet. Wait on one of those instead.
            await user.should_see(marker="assistant-model")
            user.find(marker="assistant-model").type("openai/gpt-4o")
            user.find(marker="assistant-api-key").type("sk-test")
            user.find(marker="assistant-question").type("what did I spend last month?")
            user.find(marker="assistant-send").click()
            await user.should_see("The answer is 42.")

    asyncio.run(_check())


def test_assistant_page_suggestion_sends_headline_and_detail(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The empty state's suggestions are two-line (headline + detail) but must
    send a single, fully specific question — the detail carries the real
    specificity ("by service, across every connected provider"), so dropping it
    would send a vaguer prompt than the user actually clicked on."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.assistant_engine import AssistantTurnResult
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import assistant as assistant_view

    asked: list[str] = []

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        asked.append(question)
        return AssistantTurnResult(text="Here you go.", steps=[])

    monkeypatch.setattr(assistant_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            user.find(marker="assistant-model").type("openai/gpt-4o")
            user.find(marker="assistant-api-key").type("sk-test")
            user.find(marker="assistant-suggestion-0").click()
            await user.should_see("Here you go.")

    asyncio.run(_check())

    headline, detail = assistant_view._SUGGESTIONS[0]  # noqa: SLF001
    assert asked == [f"{headline} — {detail}"]


def test_assistant_page_renders_clarify_options_as_clickable_chips(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Clicking an option chip sends its text as the next question, rather than
    requiring the user to type a reply to the model's clarifying question."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.assistant_engine import AssistantTurnResult
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import assistant as assistant_view

    sent_questions: list[str] = []

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        sent_questions.append(question)
        if len(sent_questions) == 1:
            return AssistantTurnResult(
                text="Which time window?", steps=[], options=["Last month", "Year to date"]
            )
        return AssistantTurnResult(text="Here you go.", steps=[])

    monkeypatch.setattr(assistant_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            user.find(marker="assistant-model").type("openai/gpt-4o")
            user.find(marker="assistant-api-key").type("sk-test")
            user.find(marker="assistant-question").type("what did I spend?")
            user.find(marker="assistant-send").click()
            await user.should_see("Which time window?")
            user.find(marker="assistant-option-1-0").click()
            await user.should_see("Here you go.")

    asyncio.run(_check())
    assert sent_questions == ["what did I spend?", "Last month"]


def test_infer_spec_picks_the_finer_label_when_dimensions_are_interchangeable() -> None:
    """Regression test: "visualize this spend" rendered a table, because
    query_metric returns *every* dimension of a view (no way to narrow them the
    way `measures` narrows measures) and the old heuristic demanded exactly one
    varying dimension column. Both service_category and service_name vary here,
    which is a perfectly chartable spend breakdown."""
    import pandas as pd

    from flashlight.dashboard.views.assistant import _infer_spec

    rows = [
        {"service_category": "Other", "service_name": "GENIE", "net_cost": 108.29},
        {"service_category": "Analytics", "service_name": "JOBS", "net_cost": 11961.19},
        {"service_category": "Databases", "service_name": "SQL", "net_cost": 5021.24},
    ]
    df = pd.DataFrame(rows)
    # service_category is 1:1 with service_name here, so one is dropped as
    # uninformative — it must be the coarser label that goes.
    assert _infer_spec(df, ["service_category", "service_name"]) == ("service_name", None, "bar")


def test_infer_spec_prefers_a_temporal_column_for_a_trend() -> None:
    """A month column must win even when another column is also unique, or a
    trend question silently becomes a category ranking."""
    import pandas as pd

    from flashlight.dashboard.views.assistant import _infer_spec

    df = pd.DataFrame(
        [
            {"note": "a", "charge_month": "2026-05-01", "net_cost": 1.0},
            {"note": "b", "charge_month": "2026-06-01", "net_cost": 2.0},
        ]
    )
    # note and charge_month are 1:1 here, so one is dropped as uninformative —
    # dropping the temporal one would make a trend undrawable.
    assert _infer_spec(df, ["note", "charge_month"]) == ("charge_month", None, "bar")


def test_infer_spec_draws_two_dimensions_as_a_stack_not_a_mashed_label() -> None:
    """Two dimensions with repeats in each is the stacked-bar case. It used to
    become one composite axis label per row ("JOBS · 2026-06-01"), which at real
    scale was 39 unreadable bars. A pair that repeats even together has no
    honest reading at all and must stay a table."""
    import pandas as pd

    from flashlight.dashboard.views.assistant import _infer_spec

    df = pd.DataFrame(
        [
            {"service_name": "JOBS", "charge_month": "2026-06-01", "net_cost": 10.0},
            {"service_name": "JOBS", "charge_month": "2026-07-01", "net_cost": 12.0},
            {"service_name": "SQL", "charge_month": "2026-06-01", "net_cost": 5.0},
        ]
    )
    assert _infer_spec(df, ["service_name", "charge_month"]) == (
        "service_name",
        "charge_month",
        "stacked_bar",
    )

    dupes = pd.DataFrame(
        [
            {"service_name": "JOBS", "charge_month": "2026-06-01", "net_cost": 10.0},
            {"service_name": "JOBS", "charge_month": "2026-06-01", "net_cost": 20.0},
        ]
    )
    assert _infer_spec(dupes, ["service_name", "charge_month"]) is None


def test_assistant_page_renders_a_tool_step_with_query_results(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Highest-risk new UI path: the tool-call transparency expansion actually
    renders when a turn's result carries a query_metric step with rows."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.assistant_engine import AssistantTurnResult, ToolStep
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import assistant as assistant_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        step = ToolStep(
            name="query_metric",
            arguments={"name": "aws.monthly_bill"},
            rows=[
                {"charge_month": "2026-06-01", "net_cost": 1000.0},
                {"charge_month": "2026-07-01", "net_cost": 1200.0},
            ],
        )
        return AssistantTurnResult(text="Spend rose from June to July.", steps=[step])

    monkeypatch.setattr(assistant_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            user.find(marker="assistant-model").type("openai/gpt-4o")
            user.find(marker="assistant-api-key").type("sk-test")
            user.find(marker="assistant-question").type("what did I spend?")
            user.find(marker="assistant-send").click()
            await user.should_see("Queried query_metric")
            await user.should_see("Spend rose from June to July.")

    asyncio.run(_check())


def test_assistant_page_run_sql_step_defaults_open_others_collapsed(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """run_sql is the model's own freeform SQL, not a tested view — its debug
    expansion should default open so the query is auditable at a glance,
    unlike a purpose-built tool step like query_metric."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.assistant_engine import AssistantTurnResult, ToolStep
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import assistant as assistant_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        steps = [
            ToolStep(name="list_metrics", arguments={}, rows=None),
            ToolStep(
                name="run_sql",
                arguments={"sql": "SELECT 1"},
                rows=[{"charge_month": "2026-07-01", "net_cost": 1000.0}],
            ),
        ]
        return AssistantTurnResult(text="Here you go.", steps=steps)

    monkeypatch.setattr(assistant_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            user.find(marker="assistant-model").type("openai/gpt-4o")
            user.find(marker="assistant-api-key").type("sk-test")
            user.find(marker="assistant-question").type("which service grew the most?")
            user.find(marker="assistant-send").click()
            await user.should_see("Queried run_sql")

            expansions = {e.text: e.value for e in user.find(kind=ui.expansion).elements}
            assert expansions == {
                "Called list_metrics": False,
                "Queried run_sql": True,
            }

    asyncio.run(_check())


def test_assistant_page_charts_despite_a_constant_dimension_column(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: aws.monthly_bill keeps `provider_name` as a column even
    once sliced/filtered to one provider (still constant, e.g. always "AWS") —
    that must not count as a second dimension and block the chart; only a
    column that actually varies (charge_month here) should."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.assistant_engine import AssistantTurnResult, ToolStep
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import assistant as assistant_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        step = ToolStep(
            name="query_metric",
            arguments={"name": "aws.monthly_bill", "measures": ["net_cost"]},
            rows=[
                {"provider_name": "AWS", "charge_month": "2026-06-01", "net_cost": 1000.0},
                {"provider_name": "AWS", "charge_month": "2026-07-01", "net_cost": 1200.0},
            ],
        )
        return AssistantTurnResult(text="Spend rose from June to July.", steps=[step])

    monkeypatch.setattr(assistant_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            user.find(marker="assistant-model").type("openai/gpt-4o")
            user.find(marker="assistant-api-key").type("sk-test")
            user.find(marker="assistant-question").type("chart my spend")
            user.find(marker="assistant-send").click()
            await user.should_see("Spend rose from June to July.")
            assert len(user.find(kind=ui.plotly).elements) == 1
            with pytest.raises(AssertionError):  # no table — the chart heuristic matched
                user.find(kind=ui.table)

    asyncio.run(_check())


def test_assistant_settings_persist_across_a_reload_in_the_same_tab(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Provider/model/base URL persist to config/assistant.yml (survives a process
    restart, not just a page reload — unlike app.storage.general, which they used to
    use and which the container image points at /tmp); the API key persists via the OS
    keychain (faked here with an in-memory dict — the autouse `_no_real_keyring`
    fixture blocks the real one) once the settings dialog's Done button is
    clicked, which is the only place a save is triggered."""
    import yaml
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard import assistant_credentials
    from flashlight.dashboard.router import build_pages
    from flashlight.lake import paths

    fake_keychain: dict[str, str] = {}
    monkeypatch.setattr(assistant_credentials, "_keyring_get", fake_keychain.get)
    monkeypatch.setattr(assistant_credentials, "_keyring_set", fake_keychain.__setitem__)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            provider = next(iter(user.find(marker="assistant-provider").elements))
            provider.value = "Anthropic (Claude)"  # type: ignore[attr-defined]
            user.find(marker="assistant-api-key").type("sk-persisted")
            user.find(marker="assistant-settings-done").click()

            await user.open("/assistant")  # simulate a reload within the same tab
            await user.should_see(marker="assistant-model")
            model_elem = next(iter(user.find(marker="assistant-model").elements))
            api_key_elem = next(iter(user.find(marker="assistant-api-key").elements))
            model_after_reload = model_elem.value  # type: ignore[attr-defined]
            api_key_after_reload = api_key_elem.value  # type: ignore[attr-defined]
            assert model_after_reload == "claude-sonnet-4-5"
            assert api_key_after_reload == "sk-persisted"
            assert fake_keychain == {"Anthropic (Claude)": "sk-persisted"}

            # The file is in config/ with connections.yml and policies.yml, holds the
            # internal provider id (not the dropdown label the engine can't dispatch
            # on), and carries no secret — it has to stay safe to commit or mount.
            saved = yaml.safe_load(paths.assistant_config_path().read_text())["assistant"]
            assert saved["provider"] == "anthropic"
            assert saved["model"] == "claude-sonnet-4-5"
            assert "sk-persisted" not in paths.assistant_config_path().read_text()

    asyncio.run(_check())


def test_assistant_preset_label_is_cosmetic_and_the_provider_id_is_load_bearing() -> None:
    """Three presets share `openai_compatible`, so the dropdown label can't be derived
    from the id alone — it's stored to restore the row the user picked. But it must
    never outrank the id: an env var that pins a different provider has to move the
    dropdown too, or the dialog would name a model it isn't using.
    """
    from flashlight.dashboard.assistant_config import AssistantConfig
    from flashlight.dashboard.views.assistant import _preset_label

    cfg = AssistantConfig(provider="openai_compatible", preset="Databricks")
    assert _preset_label(cfg) == "Databricks"  # noqa: SLF001 - not derivable from the id
    # Env pinned anthropic; the stale label disagrees, so the id wins.
    assert _preset_label(cfg.model_copy(update={"provider": "anthropic"})) == "Anthropic (Claude)"  # noqa: SLF001
    # Env-only config (a container) names an id and no label at all.
    assert _preset_label(AssistantConfig(provider="google")) == "Google (Gemini)"  # noqa: SLF001
    assert _preset_label(AssistantConfig()) == "OpenAI"  # noqa: SLF001 - nothing configured


def test_assistant_falls_back_to_env_var_when_keychain_has_nothing(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No keychain entry and no prior session — the FLASHLIGHT_ASSISTANT_API_KEY env
    var (same *_env indirection convention as connector credentials) prefills
    the key field."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard import assistant_credentials
    from flashlight.dashboard.router import build_pages

    monkeypatch.setenv(assistant_credentials.ENV_VAR, "sk-from-env")

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/assistant")
            await user.should_see(marker="assistant-model")
            api_key_elem = next(iter(user.find(marker="assistant-api-key").elements))
            assert api_key_elem.value == "sk-from-env"  # type: ignore[attr-defined]

    asyncio.run(_check())


def test_usage_page_renders_empty_state_with_no_assistant_activity(lake_home) -> None:  # type: ignore[no-untyped-def]
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/usage")
            await user.should_see("Usage")
            await user.should_see("No assistant activity yet")

    asyncio.run(_check())


def test_usage_page_renders_logged_assistant_turns(lake_home) -> None:  # type: ignore[no-untyped-def]
    from datetime import datetime

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake.assistant_turns import record_assistant_turn

    record_assistant_turn(
        turn_id="t1",
        session_id="s1",
        model="openai/gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        tool_call_count=1,
        occurred_at=datetime.now(UTC),
    )

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/usage")
            # ui.table renders its rows client-side from a data prop, invisible to
            # should_see's label/content matching — assert on the KPI labels instead,
            # which prove the logged row was actually read back through the DuckDB view.
            await user.should_see("Assistant turns")
            await user.should_see("Total tokens")
            # This turn was logged without timings, exactly like every turn
            # recorded before the latency columns existed: the page must say it
            # has nothing timed rather than reporting a 0-second median.
            await user.should_see("no timed turns recorded yet")

    asyncio.run(_check())


def test_usage_page_reports_where_a_timed_turn_spent_its_time(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The latency breakdown is the instrument every later optimization is judged
    against, so it has to survive a page render, not just a parquet round trip."""
    from datetime import datetime

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake.assistant_turns import record_assistant_turn

    record_assistant_turn(
        turn_id="t1",
        session_id="s1",
        model="openai/gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        tool_call_count=1,
        occurred_at=datetime.now(UTC),
        timings={
            "duration_ms": 20_000.0,
            "plan_ms": 12_000.0,
            "explore_ms": 0.0,
            "execute_ms": 40.0,
            "synthesize_ms": 7_900.0,
            "llm_request_count": 2,
            "plan_pass_count": 1,
            "empty_round_retries": 0,
            "outcome": "answer",
        },
    )

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/usage")
            await user.should_see("Median turn")
            await user.should_see("20.0s")
            await user.should_see("Where a turn's time goes")

    asyncio.run(_check())


def test_usage_page_reports_how_often_an_answer_skipped_the_llm(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Whether the deterministic answer path fires against a real model is the whole
    question the caption optimization rests on, so it's a KPI, not a table column to
    go hunting for."""
    from datetime import datetime

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake.assistant_turns import record_assistant_turn

    for turn_id, source in (("t1", "summary_spec"), ("t2", "caption"), ("t3", "model")):
        record_assistant_turn(
            turn_id=turn_id,
            session_id="s1",
            model="openai/gpt-4o",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            tool_call_count=1,
            occurred_at=datetime.now(UTC),
            timings={
                "duration_ms": 3_000.0,
                "plan_ms": 2_900.0,
                "explore_ms": 0.0,
                "execute_ms": 10.0,
                "synthesize_ms": 0.0 if source != "model" else 4_000.0,
                "llm_request_count": 1 if source != "model" else 2,
                "plan_pass_count": 1,
                "empty_round_retries": 0,
                "outcome": "answer",
                "answer_source": source,
            },
        )

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/usage")
            await user.should_see("Answered without an LLM")
            await user.should_see("67%")  # 2 of 3 skipped the synthesis call

    asyncio.run(_check())


def test_policy_page_scopes_to_the_page_date_range(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Policy Compliance has no date picker of its own — it reads the page's shared
    range, same as every other tab, so a non-compliant finding outside the selected
    window must not show, and one inside it must."""
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.efficiency.model import EfficiencyRecord, EntityType
    from flashlight.lake import bronze, metrics
    from flashlight.transform.runner import build_gold

    window = IngestWindow(_date(2026, 5, 1), _date(2026, 5, 31))
    # A Databricks cost row so the provider group (and its page) exists at all —
    # the policy tab is nested on it.
    bronze.write_window(
        "t",
        window,
        [
            FocusRecord(
                provider_name=ProviderName.DATABRICKS,
                billing_account_id="acct",
                billing_period_start=_date(2026, 5, 1),
                billing_period_end=_date(2026, 5, 31),
                charge_period_start=datetime(2026, 5, 15, tzinfo=UTC),
                charge_period_end=datetime(2026, 5, 15, 1, tzinfo=UTC),
                billed_cost=_Decimal("100"),
                effective_cost=_Decimal("100"),
                list_cost=_Decimal("100"),
                charge_category=ChargeCategory.USAGE,
                service_category=ServiceCategory.ANALYTICS,
                service_name="Databricks SQL",
                x_source_connector="t",
            )
        ],
        ingest_run_id="r1",
    )
    metrics.write_efficiency(
        window,
        [
            EfficiencyRecord(
                provider_name="Databricks",
                charge_month=_date(2026, 5, 1),
                entity_type=EntityType.INTERACTIVE,
                entity_id="cl-sluggish",
                billed_cost=_Decimal("100"),
                cause_detail={"auto_termination_minutes": 600},
                x_source_connector="databricks",
            )
        ],
    )
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("Policy Compliance")
            user.find(kind=ui.tab, content="Policy Compliance").click()
            # Default range is YTD, which covers May 2026 — the finding is visible.
            # (Table cell contents render client-side and aren't visible to
            # user_simulation — the KPI row is, so that's what's asserted on.)
            await user.should_see("Non-compliant entities")
            await user.should_see("0 of 3 measured")
            # No static thresholds panel — that config lives in policies.yml, not here.
            await user.should_not_see("Policy thresholds")

    asyncio.run(_check())


def test_policy_summary_aggregates_to_the_policy_grain() -> None:
    """`_policy_summary` — the pure computation behind the "Non-compliant entities"
    drill-through's first level. Not testable through `user_simulation` (`ui.table` row
    clicks are a client-side Quasar event, same caveat as `attribution.py`'s drill-through),
    so it's pinned directly here instead.

    Two entities, two categories: `auto_terminate` has one non-compliant and one
    compliant (50%); `cluster_tagging` is entirely `not_applicable` (tag telemetry
    unmeasured), so it must show 0 measured and a NaN — never a 0% — compliance rate.
    """
    import pandas as pd

    from flashlight.dashboard.views import policy

    records = pd.DataFrame(
        [
            {"policy_category": "auto_terminate", "status": "non_compliant"},
            {"policy_category": "auto_terminate", "status": "compliant"},
            {"policy_category": "cluster_tagging", "status": "not_applicable"},
        ]
    )
    summary = policy._policy_summary(records).set_index("policy_category")  # noqa: SLF001

    auto_terminate = summary.loc["auto_terminate"]
    assert auto_terminate["policy_label"] == "Auto-termination policy"
    assert (auto_terminate["non_compliant"], auto_terminate["compliant"]) == (1, 1)
    assert auto_terminate["not_evaluated"] == 0
    assert auto_terminate["compliance_pct"] == 50.0

    tagging = summary.loc["cluster_tagging"]
    assert (tagging["non_compliant"], tagging["compliant"], tagging["not_evaluated"]) == (0, 0, 1)
    assert pd.isna(tagging["compliance_pct"]), "no measured entities ⇒ no rate, not 0%"

    # Ranked by non_compliant descending — the worse policy leads.
    ranked = policy._policy_summary(records)  # noqa: SLF001
    assert ranked.iloc[0]["policy_category"] == "auto_terminate"


def test_policy_latest_per_entity_collapses_repeat_months() -> None:
    """Regression test: a cluster non-compliant for 6 straight months must show up as
    ONE row, not 6 — real data on a YTD range showed the same cluster/detail repeated
    once per month it had been misconfigured, which read as duplicate rows rather than
    a single distinct finding.

    `_latest_per_entity` keeps each (entity_id, policy_category)'s most recent row —
    pinned here by checking it keeps May's (not January's) detail for the entity that
    appears in both months, and never inflates the entity count.
    """
    import pandas as pd

    from flashlight.dashboard.views import policy

    records = pd.DataFrame(
        [
            {
                "entity_id": "cl-1",
                "policy_category": "auto_terminate",
                "charge_month": "2026-01-01",
                "status": "non_compliant",
                "detail": "auto-terminate after 600 min, over the 60 min policy",
            },
            {
                "entity_id": "cl-1",
                "policy_category": "auto_terminate",
                "charge_month": "2026-05-01",
                "status": "non_compliant",
                "detail": "auto-terminate after 600 min, over the 60 min policy",
            },
            {
                "entity_id": "cl-2",
                "policy_category": "auto_terminate",
                "charge_month": "2026-03-01",
                "status": "compliant",
                "detail": "auto-terminate after 30 min",
            },
        ]
    )
    latest = policy._latest_per_entity(records)  # noqa: SLF001

    assert len(latest) == 2, "cl-1's two monthly rows collapse into one"
    assert set(latest["entity_id"]) == {"cl-1", "cl-2"}
    cl1 = latest[latest["entity_id"] == "cl-1"].iloc[0]
    assert cl1["charge_month"] == "2026-05-01", "keeps the most recent month, not the first"


def test_policy_row_click_recovers_category_without_the_raw_column() -> None:
    """Regression test for a dashboard crash (`KeyError: 'policy_category'`) on every
    click of the policy-grain table in a real `dashboard serve` run.

    `chrome.searchable_table`'s `on_row_click` hands the handler a dict built from
    exactly the DataFrame that was passed in — here, `summary[_POLICY_COLS]` — which
    carries `policy_label` (the displayed text) but never the raw `policy_category`
    (it isn't one of `_POLICY_COLS`). Reading `row["policy_category"]` in the click
    handler therefore always raised; recovering the category has to go through the
    label instead.
    """
    from flashlight.dashboard.views import policy

    for category, label in policy._CATEGORY_LABEL.items():  # noqa: SLF001
        # Exactly the shape a real click hands the handler: only `_POLICY_COLS` keys.
        row: dict[str, object] = {
            "policy_label": label,
            "non_compliant": 1,
            "compliant": 0,
            "not_evaluated": 0,
            "compliance_pct": 0.0,
        }
        assert "policy_category" not in row
        assert policy._category_from_label(str(row["policy_label"])) == category  # noqa: SLF001


def test_policy_rule_labels_are_unique() -> None:
    """The row-click reverse lookup (`_category_from_label`) is only safe if no two
    policy rules share a label — otherwise clicking one would silently drill into the
    other's entities."""
    from flashlight.efficiency.policy_rules import POLICY_RULES

    labels = [r.label for r in POLICY_RULES]
    assert len(labels) == len(set(labels))


def test_policy_non_compliant_panel_opens_at_the_policy_grain(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The drill-through opens on the policy summary, not a flat entity list — matching
    Attribution's "Untagged infrastructure" pattern of leading with the coarser grain."""
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.efficiency.model import EfficiencyRecord, EntityType
    from flashlight.lake import bronze, metrics
    from flashlight.transform.runner import build_gold

    window = IngestWindow(_date(2026, 5, 1), _date(2026, 5, 31))
    bronze.write_window(
        "t",
        window,
        [
            FocusRecord(
                provider_name=ProviderName.DATABRICKS,
                billing_account_id="acct",
                billing_period_start=_date(2026, 5, 1),
                billing_period_end=_date(2026, 5, 31),
                charge_period_start=datetime(2026, 5, 15, tzinfo=UTC),
                charge_period_end=datetime(2026, 5, 15, 1, tzinfo=UTC),
                billed_cost=_Decimal("100"),
                effective_cost=_Decimal("100"),
                list_cost=_Decimal("100"),
                charge_category=ChargeCategory.USAGE,
                service_category=ServiceCategory.ANALYTICS,
                service_name="Databricks SQL",
                x_source_connector="t",
            )
        ],
        ingest_run_id="r1",
    )
    metrics.write_efficiency(
        window,
        [
            EfficiencyRecord(
                provider_name="Databricks",
                charge_month=_date(2026, 5, 1),
                entity_type=EntityType.INTERACTIVE,
                entity_id="cl-sluggish",
                billed_cost=_Decimal("100"),
                cause_detail={"auto_termination_minutes": 600},
                x_source_connector="databricks",
            )
        ],
    )
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="Policy Compliance").click()
            await user.should_see("Click a policy to see the entities behind it.")
            # Entity-level breadcrumb text must NOT be showing yet — confirms the
            # panel opened at the policy grain, not already drilled in.
            await user.should_not_see("← All policies")

    asyncio.run(_check())


def test_resolve_chart_honours_a_declared_stacked_bar() -> None:
    """Regression test: "visualize databricks spend year to date by service"
    returned service x month rows and the shape-inference mashed both dimensions
    into one axis, drawing 39 bars labelled "Networking · NETWORKING ·
    2026-07-01". Shape can't carry intent — the same columns mean x=service_name
    for that question and x=charge_month for "the monthly trend" — so the model
    declares it (see ChartSpec)."""
    import pandas as pd

    from flashlight.dashboard.assistant_engine import ChartSpec
    from flashlight.dashboard.views.assistant import _resolve_chart

    df = pd.DataFrame(
        [
            {"service_name": s, "charge_month": m, "net_cost": 1.0}
            for s in ("JOBS", "SQL")
            for m in ("2026-06-01", "2026-07-01")
        ]
    )
    spec = ChartSpec(kind="stacked_bar", x="service_name", series="charge_month")
    assert _resolve_chart(df, spec, ["net_cost"]) == ("service_name", "charge_month", "stacked_bar")
    # Same rows, other intent — the model's declaration is what differs.
    trend = ChartSpec(kind="stacked_bar", x="charge_month", series="service_name")
    expected = ("charge_month", "service_name", "stacked_bar")
    assert _resolve_chart(df, trend, ["net_cost"]) == expected


@pytest.mark.parametrize(
    ("label", "spec_kwargs"),
    [
        # The model names columns from the catalog in its prompt, not from the
        # result, so it can name one this query didn't return.
        ("unknown x", {"x": "nope"}),
        ("unknown series", {"x": "service_name", "series": "nope"}),
        # x alone repeats (one row per service *per month*): drawing it would put
        # several segments on one tick, reading as though one were the total.
        ("x is not unique", {"x": "service_name"}),
    ],
)
def test_resolve_chart_rejects_what_it_cannot_honour(label: str, spec_kwargs: dict) -> None:  # type: ignore[type-arg]
    import pandas as pd

    from flashlight.dashboard.assistant_engine import ChartSpec
    from flashlight.dashboard.views.assistant import _resolve_chart

    df = pd.DataFrame(
        [
            {"service_name": s, "charge_month": m, "net_cost": 1.0}
            for s in ("JOBS", "SQL")
            for m in ("2026-06-01", "2026-07-01")
        ]
    )
    assert _resolve_chart(df, ChartSpec(**spec_kwargs), ["net_cost"]) is None, label


def test_cap_series_folds_the_tail_into_other_without_losing_total() -> None:
    """More series than palette slots is unreadable whatever the colours, but
    nothing may be dropped — the folded bucket must still carry its spend."""
    import pandas as pd

    from flashlight.dashboard import chrome

    df = pd.DataFrame(
        [{"service_name": f"svc{i}", "net_cost": float(i)} for i in range(chrome.MAX_SERIES + 5)]
    )
    capped = chrome.cap_series(df, "service_name", "net_cost")
    assert capped["service_name"].nunique() == chrome.MAX_SERIES
    assert "Other" in set(capped["service_name"])
    assert capped["net_cost"].sum() == df["net_cost"].sum()


def test_infer_spec_falls_back_to_a_stacked_bar_for_service_by_month() -> None:
    """The deterministic floor under the declaration, and the case that produced
    the bug: a live gpt-oss-20b declared a chart for "show me the monthly trend"
    but *not* for "visualize databricks spend year to date by service". With no
    declaration the shape must still read as service x month, not as 39 bars
    labelled "Networking · NETWORKING · 2026-07-01".

    service_category is dropped as uninformative: it varies, but every service
    has exactly one category, so it splits nothing and would otherwise count as
    a third dimension and force a table.
    """
    import pandas as pd

    from flashlight.dashboard.views.assistant import _infer_spec, _informative_dims

    df = pd.DataFrame(
        [
            {
                "provider_name": "Databricks",
                "service_category": c,
                "service_name": s,
                "charge_month": m,
                "net_cost": 1.0,
            }
            for c, s in (("Analytics", "JOBS"), ("Databases", "SQL"), ("Other", "APPS"))
            for m in ("2026-05-01", "2026-06-01", "2026-07-01")
        ]
    )
    varying = [c for c in df.columns if c != "net_cost" and df[c].nunique() > 1]
    assert _informative_dims(df, varying) == ["service_name", "charge_month"]
    # Wider dimension on x so the stack stays palette-sized: services split by
    # months, not months split by services.
    assert _infer_spec(df, varying) == ("service_name", "charge_month", "stacked_bar")


def test_infer_spec_stays_a_table_when_there_is_no_honest_two_dimension_reading() -> None:
    """Three genuinely independent dimensions can't be drawn in 2-D without
    hiding one, so it stays a table rather than pretending."""
    import pandas as pd

    from flashlight.dashboard.views.assistant import _infer_spec

    df = pd.DataFrame(
        [{"a": a, "b": b, "c": c, "v": 1.0} for a in "xy" for b in "pq" for c in "12"]
    )
    assert _infer_spec(df, ["a", "b", "c"]) is None


def test_plot_stacks_a_bar_with_a_series_however_the_kind_was_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live model declared kind="bar" with series="charge_month" for a
    year-to-date breakdown; grouping that put 8 months x 13 services side by side
    as ~104 thin bars. Spend is additive, so with a series present the stack —
    whose total is the number the question was about — is always the right read.
    """
    import pandas as pd
    import plotly.express as px

    from flashlight.dashboard import chrome
    from flashlight.dashboard.views import assistant as assistant_view

    captured: list[dict[str, object]] = []
    real_bar = px.bar

    def spy_bar(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        captured.append(kwargs)
        return real_bar(*args, **kwargs)

    # monkeypatch (not bare assignment) so both globals are restored even if this
    # test fails — chrome.plot is shared module state that every chart test uses.
    monkeypatch.setattr(px, "bar", spy_bar)
    monkeypatch.setattr(chrome, "plot", lambda fig, **kwargs: None)

    df = pd.DataFrame(
        [
            {"service_name": s, "charge_month": m, "net_cost": 1.0}
            for s in ("JOBS", "SQL")
            for m in ("2026-06-01", "2026-07-01")
        ]
    )
    assistant_view._plot(df, ("service_name", "charge_month", "bar"), "net_cost")  # noqa: SLF001
    assert captured
    assert captured[0]["barmode"] == "stack"


# ── Provider "Attribution" / "Efficiency & Waste" tabs ─────────────────────
# These were the cross-provider /leaderboard and /utilization pages. They're tabs on
# every provider page now, so each test opens a provider page instead of a page of its
# own. No tab click: NiceGUI builds every tab panel's content up front, so should_see
# reaches an inactive panel (same as the redshift Optimization assertions above).
def _db_cost_row() -> FocusRecord:
    """A Databricks cost row, so the provider group exists and has_data() is true."""
    return FocusRecord(
        provider_name=ProviderName.DATABRICKS,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=datetime(2026, 5, 15, tzinfo=UTC),
        charge_period_end=datetime(2026, 5, 15, 1, tzinfo=UTC),
        billed_cost=Decimal("100"),
        effective_cost=Decimal("100"),
        list_cost=Decimal("100"),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.ANALYTICS,
        service_name="Databricks SQL",
        tags={"Epic": "growth"},
        x_source_connector="t",
    )


def _seed(
    efficiency_records: list[EfficiencyRecord], *, cost_rows: list[FocusRecord] | None = None
) -> None:
    from flashlight.lake import bronze, metrics
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, cost_rows or [_db_cost_row()], ingest_run_id="r1")
    if efficiency_records:
        metrics.write_efficiency(window, efficiency_records)
    build_gold()


def _eff(entity_id: str, entity_type: EntityType, cost: str, **kw: object) -> EfficiencyRecord:
    return EfficiencyRecord(
        provider_name="Databricks",
        charge_month=date(2026, 5, 1),
        entity_type=entity_type,
        entity_id=entity_id,
        billed_cost=Decimal(cost),
        x_source_connector="databricks",
        **kw,
    )


_CORE_TABS = (
    "Trend & changes",
    "Breakdown",
    "Attribution",
    "Efficiency & Waste",
    "Policy Compliance",
)

_ALERTS_TAB = "Alerts"

# Databricks: spend detail after Breakdown, Client Driver Health last.
_DATABRICKS_TABS = (
    "Trend & changes",
    "Breakdown",
    "AI Costs",
    "Databricks Storage",
    "Databricks Compute",
    "Attribution",
    "Efficiency & Waste",
    "Policy Compliance",
    "Client Driver Health",
)


def test_provider_page_carries_the_core_tabs(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Every provider page has the same tab set — this is what stops /aws drifting back to
    its old Tags/Optimization pair, Efficiency & Waste becoming Databricks-only again, or
    Policy Compliance going back to being a Databricks extra tab (which hid rows every
    Redshift cluster-month was already producing).

    Loops the *discovered* groups rather than a hard-coded pair, so a provider added later
    can't quietly get a different set. GCP is seeded deliberately: a connector that pulls
    FOCUS cost and nothing else must still show all core tabs and Alerts, with named empty
    states. Databricks deliberately omits Alerts.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.transform.catalog import discover_provider_groups

    aws = _rec(15)
    aws.service_name = "Amazon Redshift"
    gcp = _rec(15)
    gcp.provider_name = ProviderName.GCP
    _seed([], cost_rows=[_db_cost_row(), aws, gcp])

    from flashlight.dashboard.router import build_pages

    groups = discover_provider_groups()
    assert set(groups) == {"aws", "databricks", "gcp"}, "fixture should cover all three shapes"

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            for group in groups:
                await user.open(f"/{group}")
                for tab in _CORE_TABS:
                    await user.should_see(tab)
                if group == "databricks":
                    await user.should_not_see(_ALERTS_TAB)
                else:
                    await user.should_see(_ALERTS_TAB)

    asyncio.run(_check())


def test_databricks_tab_order_puts_spend_detail_after_breakdown(lake_home) -> None:  # type: ignore[no-untyped-def]
    """AI Costs / Storage sit next to Breakdown; Alerts are absent.

    Extras used to append after Policy, so the most-asked spend questions scrolled off
    the tab bar. Order is the contract — presence alone is covered by the core-tabs test.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed([], cost_rows=[_db_cost_row()])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            # find(kind=…) is unordered; creation id tracks declaration order.
            labels = [t.label for t in sorted(user.find(kind=ui.tab).elements, key=lambda t: t.id)]
            assert labels == list(_DATABRICKS_TABS), labels

    asyncio.run(_check())


def test_alerts_tab_holds_mom_prose_not_the_kpi_header(lake_home) -> None:  # type: ignore[no-untyped-def]
    """MoM callout used to sit under the KPI row; it belongs on Alerts now.

    Asserts both halves: the landing panel no longer carries the old "in the selected
    window" header caption, and the Alerts tab surfaces the window total.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    april = _db_cost_row()
    april.provider_name = ProviderName.GCP
    april.billing_period_start = date(2026, 4, 1)
    april.billing_period_end = date(2026, 4, 30)
    april.charge_period_start = datetime(2026, 4, 15, tzinfo=UTC)
    april.charge_period_end = datetime(2026, 4, 15, 1, tzinfo=UTC)
    april.billed_cost = april.effective_cost = april.list_cost = Decimal("80")
    may = _db_cost_row()
    may.provider_name = ProviderName.GCP
    may.billed_cost = may.effective_cost = may.list_cost = Decimal("100")
    bronze.write_window(
        "t",
        IngestWindow(date(2026, 4, 1), date(2026, 5, 31)),
        [april, may],
        ingest_run_id="r1",
    )
    build_gold()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/gcp")
            await user.should_see("Net Spend")
            await user.should_see("Alerts")
            await user.should_not_see("in the selected window")
            user.find(kind=ui.tab, content="Alerts").click()
            await user.should_see("Selected window")
            await user.should_see("Top Service movers")

    asyncio.run(_check())


def test_efficiency_tab_states_its_measurement_coverage(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Coverage is the honesty frame the old /utilization page led with, kept as one line:
    'not applicable' and 'unmeasured' must be visible, a reading pegged at 100% must read as
    a ceiling rather than praise, and an unflagged entity must not read as a clean bill."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed(
        [
            _eff("job-fine", EntityType.JOB, "100", utilization_pct=75.0, activity_count=5),
            _eff("job-pegged", EntityType.JOB, "100", utilization_pct=100.0, activity_count=5),
            _eff("nb-silent", EntityType.NOTEBOOK, "40"),
            _eff("wh-shared", EntityType.SQL_WAREHOUSE, "80", cause_detail={"cache_hit_pct": 12.0}),
        ]
    )

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="Efficiency & Waste").click()
            # 2 of the 4 entity-months carry a reading; the other two are one
            # not-applicable (shared warehouse) and one unmeasured (silent notebook).
            await user.should_see("2 of 4 entity-months")
            await user.should_see("none obtainable in principle")
            await user.should_see("no telemetry arrived")
            await user.should_see("sensor ceiling")
            await user.should_see("unflagged, not proven efficient")

    asyncio.run(_check())


def _db_service_row(service_name: str, cost: str) -> FocusRecord:
    rec = _db_cost_row()
    rec.service_name = service_name
    rec.billed_cost = rec.effective_cost = rec.list_cost = Decimal(cost)
    return rec


def test_databricks_monthly_bar_splits_by_service_and_still_reconciles(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The Databricks monthly bar stacks by service so a month's movement is legible
    before anyone clicks — but the stack's total must stay the bill: spend_by_service_month
    is the same sum(cost) as monthly_bill, only grouped finer, and a stack that didn't
    reconcile would silently restate the number the panel title promises.

    Other providers stay flat: on /aws a credit is a negative service row and would draw
    a segment below zero on the one chart meant to read as "the bill".
    """
    from flashlight.dashboard.data import gold_df
    from flashlight.dashboard.views import provider_focus

    aws = _rec(15)
    aws.service_name = "Amazon Redshift"
    _seed(
        [],
        cost_rows=[
            _db_service_row("Databricks Jobs Compute", "300"),
            _db_service_row("Databricks Serverless SQL", "100"),
            aws,
        ],
    )

    sm, end = date(2026, 5, 1), date(2026, 5, 31)
    stacked = provider_focus._monthly_by_service(  # noqa: SLF001
        provider_focus.Scope("databricks"), end, sm
    )
    assert set(stacked["service_name"]) == {
        "Databricks Jobs Compute",
        "Databricks Serverless SQL",
    }
    bill = float(gold_df('SELECT sum(net_cost) AS c FROM "databricks".monthly_bill')["c"].iloc[0])
    assert stacked["net_cost"].sum() == pytest.approx(bill)
    # Biggest service first, so a segment keeps its colour and position across months.
    assert provider_focus._service_order(stacked)[0] == "Databricks Jobs Compute"  # noqa: SLF001
    # AWS opts out → the caller falls back to the flat monthly bar.
    assert provider_focus._monthly_by_service(  # noqa: SLF001
        provider_focus.Scope("aws"), end, sm
    ).empty


def test_efficiency_tab_names_missing_telemetry_instead_of_hiding_the_tab(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A provider whose connector pulls FOCUS cost only still gets the tab, with an empty
    state that names the fix — hiding it would make 'never measured' look like 'nothing to
    find'."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    gcp = _rec(15)
    gcp.provider_name = ProviderName.GCP
    _seed([], cost_rows=[gcp])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/gcp")
            await user.should_see("Efficiency & Waste")
            user.find(kind=ui.tab, content="Efficiency & Waste").click()
            await user.should_see("No efficiency telemetry for GCP")
            await user.should_see("Databricks connector")
            await user.should_see("coverage gap, not a verdict")

    asyncio.run(_check())


def test_attribution_tier_for_service_matches_real_billing_grain() -> None:
    """The billing-granularity tier a service falls into (attribution.py's module
    docstring): dedicated (resource_id IS the billed unit), shared+sub-metered (a SQL
    warehouse/Redshift cluster — a per-user estimate exists), shared with no sub-grain
    (all-purpose/interactive clusters), or unclassified (fall back to the generic
    remedy rather than guess). Pinned directly against ``billing_origin_product``
    strings (see ``databricks_focus_1_3.sql``'s ``ServiceName`` — verbatim, not a
    human label) and :data:`REDSHIFT_SERVICE_NAMES`, so a real Databricks/Redshift bill
    lands where the module docstring claims.
    """
    from flashlight.dashboard.views import attribution

    for service in ("JOBS", "DLT", "MODEL_SERVING", "NOTEBOOKS"):
        assert attribution._tier_for_service(service) == "dedicated", service  # noqa: SLF001
    for service in ("SQL", "Amazon Redshift", "Amazon Redshift Spectrum"):
        assert attribution._tier_for_service(service) == "shared_subgrain", service  # noqa: SLF001
    for service in ("ALL_PURPOSE", "INTERACTIVE", "SHARED_SERVERLESS_COMPUTE"):
        assert (
            attribution._tier_for_service(service) == "shared_no_subgrain"  # noqa: SLF001
        ), service
    for service in ("DATABASE", "NETWORKING", "AmazonS3"):
        assert attribution._tier_for_service(service) == "unclassified", service  # noqa: SLF001


def test_attribution_tab_leads_with_cost_attribution(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Attribution's one drill-through panel — the service level, on first render.

    Replaces the old two-stacked-panels design (a standing "Untagged resources" table
    below the service table): now there's exactly one panel, and the resource/driver
    levels only render after a service is clicked — not testable through
    ``user_simulation`` (``ui.table`` row clicks are a client-side Quasar event; see the
    "renders rows client-side, invisible to should_see" note elsewhere in this file).
    The resource-level query and the driver-level join are pinned directly against GOLD
    in :func:`test_attribution_untagged_resource_and_driver_views_reconcile` instead.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    tagged = _db_cost_row()
    untagged = _db_cost_row()
    untagged.tags = {}
    untagged.service_name = "JOBS"
    untagged.resource_id = "job-untagged"
    untagged.resource_name = "nightly-etl"
    untagged.billed_cost = untagged.effective_cost = untagged.list_cost = Decimal("50")

    _seed(
        [_eff("job-idle", EntityType.JOB, "20", activity_count=0, owner_user="carol")],
        cost_rows=[tagged, untagged],
    )

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="Attribution").click()
            await user.should_see("Cost attribution")
            # Cost accountability leads; customer-defined tag allocation is available
            # as a separate drill rather than competing with the service hierarchy.
            await user.should_see("Tag-based attribution")
            await user.should_not_see("Untagged infrastructure")

    asyncio.run(_check())


def test_attribution_untagged_resource_and_driver_views_reconcile(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The two GOLD queries the resource and driver levels read, without a browser click.

    A shared+sub-metered resource (SQL warehouse) drills to its estimated per-user
    drivers via ``entity_id LIKE '<resource_id>:%'`` on
    ``efficiency.utilization_entity_month`` — this pins that join actually matches on
    real data, and that a *different* warehouse's driver rows are excluded (a bare
    substring match would also catch "wh-shared-2:...").
    """
    from flashlight.dashboard.data import gold_df
    from flashlight.lake import bronze, metrics
    from flashlight.transform.runner import build_gold

    untagged = _db_cost_row()
    untagged.tags = {}
    untagged.service_name = "SQL"
    untagged.resource_id = "wh-shared"
    untagged.resource_name = "Shared Warehouse"
    untagged.billed_cost = untagged.effective_cost = untagged.list_cost = Decimal("80")

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [untagged], ingest_run_id="r1")
    metrics.write_efficiency(
        window,
        [
            EfficiencyRecord(
                provider_name="Databricks",
                charge_month=date(2026, 5, 1),
                entity_type=EntityType.SQL_WAREHOUSE_USER,
                entity_id="wh-shared:carol",
                owner_user="carol",
                billed_cost=Decimal("60"),
                cause_detail={"duration_share_pct": 75.0, "query_count": 12},
                x_source_connector="databricks",
            ),
            # A different warehouse — must NOT show up under "wh-shared"'s drivers
            # (a bare substring LIKE '%wh-shared%' would wrongly catch this one).
            EfficiencyRecord(
                provider_name="Databricks",
                charge_month=date(2026, 5, 1),
                entity_type=EntityType.SQL_WAREHOUSE_USER,
                entity_id="wh-shared-2:dave",
                owner_user="dave",
                billed_cost=Decimal("99"),
                cause_detail={"duration_share_pct": 90.0},
                x_source_connector="databricks",
            ),
        ],
    )
    build_gold()

    res = gold_df(
        "SELECT resource_id, sum(gross_cost) AS gross_cost "
        'FROM "databricks".resource_month '
        "WHERE service_name = 'SQL' GROUP BY resource_id"
    )
    assert res.loc[res["resource_id"] == "wh-shared", "gross_cost"].iloc[0] == pytest.approx(
        80.0
    )
    service_total = gold_df(
        "SELECT sum(gross_cost) AS gross_cost FROM \"databricks\".spend_by_service_month "
        "WHERE service_name = 'SQL'"
    )
    resource_total = float(res["gross_cost"].sum())
    assert resource_total == pytest.approx(float(service_total["gross_cost"].iloc[0]))

    drivers = gold_df(
        "SELECT owner_user, billed_cost, primary_signal_value AS duration_share_pct "
        "FROM efficiency.utilization_entity_month "
        "WHERE provider_name = 'Databricks' AND entity_type = 'sql_warehouse_user' "
        "AND entity_id LIKE 'wh-shared:%'"
    )
    assert list(drivers["owner_user"]) == ["carol"]
    assert float(drivers["billed_cost"].iloc[0]) == pytest.approx(60.0)
    assert float(drivers["duration_share_pct"].iloc[0]) == pytest.approx(75.0)

    # Attribution allocates from the resource charge (80), not telemetry's source
    # cost (60), and makes the telemetry-free remainder visible.  Thus the children
    # reconcile exactly to the selected warehouse even when sources differ.
    allocation = gold_df(
        "WITH resource_cost AS ("
        " SELECT sum(gross_cost) AS resource_cost FROM \"databricks\".resource_month "
        " WHERE service_name = 'SQL' AND resource_id = 'wh-shared'"
        "), shares AS ("
        " SELECT owner_user, primary_signal_value / 100.0 AS duration_share "
        " FROM efficiency.utilization_entity_month "
        " WHERE provider_name = 'Databricks' AND entity_type = 'sql_warehouse_user' "
        " AND entity_id LIKE 'wh-shared:%'"
        ") SELECT sum(resource_cost * duration_share) AS allocated_cost "
        " FROM resource_cost CROSS JOIN shares"
    )
    assert float(allocation["allocated_cost"].iloc[0]) == pytest.approx(60.0)
    assert 80.0 - float(allocation["allocated_cost"].iloc[0]) == pytest.approx(20.0)


def test_attribution_tag_value_level_reads_the_folded_key(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The GOLD query :func:`attribution._render_tag_value_level` runs, without a
    browser click: picking the *folded* key ("Epic") must still surface dollars raw-
    tagged with a case/separator variant ("epic"), the same fold
    ``spend_by_tag_key_month`` already applied to rank it.
    """
    from flashlight.dashboard.data import gold_df

    growth = _db_cost_row()
    growth.tags = {"Epic": "growth"}
    growth.billed_cost = growth.effective_cost = growth.list_cost = Decimal("30")
    platform = _db_cost_row()
    platform.tags = {"epic": "platform"}
    platform.billed_cost = platform.effective_cost = platform.list_cost = Decimal("10")

    _seed([], cost_rows=[growth, platform])

    values = gold_df(
        "SELECT tag_value, sum(net_cost) AS net_cost FROM "
        '"databricks".spend_by_tag_month '
        "WHERE replace(lower(trim(tag_key)), '-', '_') = 'epic' "
        "GROUP BY tag_value ORDER BY net_cost DESC"
    )
    assert set(values["tag_value"]) == {"growth", "platform"}
    assert float(values.loc[values["tag_value"] == "growth", "net_cost"].iloc[0]) == (
        pytest.approx(30.0)
    )


def test_provider_page_hides_the_projection_on_one_day_of_history(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A single day of data must not produce a Projected tile — it was reading ~30x high."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    # GCP → the plain provider_focus page, where the KPI row lives.
    row = _rec(15)
    row.provider_name = ProviderName.GCP
    bronze.write_window(
        "t", IngestWindow(date(2026, 5, 15), date(2026, 5, 15)), [row], ingest_run_id="r1"
    )
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/gcp")
            await user.should_see("GCP spend")
            await user.should_not_see("Projected")
            await user.should_not_see("Month to date")

    asyncio.run(_check())


def _seed_daily_databricks(days: int, *, first: date = date(2026, 1, 1)) -> None:
    """*days* consecutive daily Databricks rows — enough history (≥3 complete months)
    that `spend_forecast_month` emits a real trend instead of NULLing it."""
    from datetime import timedelta

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    rows = []
    for i in range(days):
        row = _rec(1)
        row.provider_name = ProviderName.DATABRICKS
        row.service_name = "JOBS"
        row.service_category = ServiceCategory.ANALYTICS
        when = datetime(first.year, first.month, first.day, tzinfo=UTC) + timedelta(days=i)
        row.charge_period_start = when
        row.charge_period_end = when
        rows.append(row)
    last = first + timedelta(days=days - 1)
    bronze.write_window("t", IngestWindow(first, last), rows, ingest_run_id="r1")
    build_gold()


def test_monthly_chart_carries_the_forecast_on_the_same_axis(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Actuals and the 3-month forecast are one chart, not two panels.

    As separate panels the forecast had its own y-scale, which drew a $14K projection as a
    taller bar than a $40K actual month. Sharing the axis fixes the comparison — and the
    forecast trace stays distinguishable (its own name, hatched, no `customdata`), because
    a projection that looks measured is worse than one drawn in its own panel.
    """
    from typing import cast

    import plotly.graph_objects as go
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views.provider_focus import FORECAST_SERIES

    # 121 days from Jan 1 ends on May 1, so the last *complete* day is Apr 30 and the trend
    # projects May–Jul. May already has an actual bar, so only two forecast bars are drawn —
    # stacking a projection onto a measured month would read as one part-invented total.
    _seed_daily_databricks(121)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("Monthly net cost & 3-month forecast")
            await user.should_not_see("click a bar to drill in")
            await user.should_not_see("Hatched bars are the next")
            # The standalone forecast panel is gone.
            await user.should_not_see("Next 3 months — trend forecast")

            # ui.plotly.figure is typed `go.Figure | dict`; every chart here builds a Figure.
            figures = [cast(go.Figure, e.figure) for e in user.find(kind=ui.plotly).elements]
            with_forecast = [
                f
                for f in figures
                if any(getattr(t, "name", None) == FORECAST_SERIES for t in f.data)
            ]
            assert len(with_forecast) == 1, "the forecast should be drawn exactly once"
            fig = with_forecast[0]
            forecast = next(t for t in fig.data if t.name == FORECAST_SERIES)
            actual = [t for t in fig.data if t.name != FORECAST_SERIES]
            # Same figure ⇒ same y-axis. This is the whole point of the merge.
            assert actual, "the forecast must share the figure with the actual months"
            # A click on a forecast bar has no month to drill into, and says so by carrying
            # no customdata — see _monthly_drill's _on_click.
            assert forecast.customdata is None
            assert all(t.customdata is not None for t in actual)
            assert min(forecast.x) > max(str(m) for t in actual for m in t.x), (
                "forecast months must sit to the right of every actual month"
            )
            assert len(forecast.x) == 2, (
                "May has actuals, so only Jun and Jul are projected — a forecast bar must "
                f"never stack onto a measured month: {forecast.x}"
            )

    asyncio.run(_check())


def test_monthly_chart_labels_the_stack_total(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A service-stacked bar gets one total label — the full stack height — since
    reading the month's total off 6-8 thin segments by eye isn't realistic."""
    from typing import cast

    import plotly.graph_objects as go
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.theme import compact_money
    from flashlight.dashboard.views.provider_focus import FORECAST_SERIES

    _seed_daily_databricks(121)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")

            figures = [cast(go.Figure, e.figure) for e in user.find(kind=ui.plotly).elements]
            with_forecast = [
                f
                for f in figures
                if any(getattr(t, "name", None) == FORECAST_SERIES for t in f.data)
            ]
            assert len(with_forecast) == 1
            fig = with_forecast[0]

            # The stack height per month, summed straight from the traces the chart
            # actually drew — actuals plus whatever's projected on top of them.
            totals: dict[str, float] = {}
            for t in fig.data:
                for x, y in zip(t.x, t.y, strict=True):
                    totals[x] = totals.get(x, 0.0) + float(y)

            annotations = {a.x: a.text for a in fig.layout.annotations}
            assert set(annotations) == set(totals), "every bar needs exactly one total label"
            for month, total in totals.items():
                assert annotations[month] == compact_money(total)

    asyncio.run(_check())


def test_monthly_chart_stacks_a_projected_remainder_on_the_partial_month(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The current, still-accruing month gets realized + projected — not a sliver.

    217 days from Jan 1 ends Aug 5, so the last complete day is Aug 4: August itself
    carries only 4 days of history (>= the 3-day run-rate floor) on top of 7 full months,
    so both the trend (Sep-Nov) *and* the run-rate remainder for August should render.
    """
    from typing import cast

    import plotly.graph_objects as go
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views.provider_focus import FORECAST_SERIES

    _seed_daily_databricks(217)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("Monthly net cost & 3-month forecast")

            figures = [cast(go.Figure, e.figure) for e in user.find(kind=ui.plotly).elements]
            with_forecast = [
                f
                for f in figures
                if any(getattr(t, "name", None) == FORECAST_SERIES for t in f.data)
            ]
            assert len(with_forecast) == 1
            fig = with_forecast[0]
            forecast_traces = [t for t in fig.data if t.name == FORECAST_SERIES]
            actual_traces = [t for t in fig.data if t.name != FORECAST_SERIES]

            # Two forecast traces: the 3-month trend (Sep-Nov) and August's remainder.
            assert len(forecast_traces) == 2, forecast_traces
            remainder_trace = next(t for t in forecast_traces if "2026-08" in t.x)
            trend_trace = next(t for t in forecast_traces if "2026-08" not in t.x)
            assert list(remainder_trace.x) == ["2026-08"]
            assert set(trend_trace.x) == {"2026-09", "2026-10", "2026-11"}
            # Still inert on click — no month behind either projected segment.
            assert remainder_trace.customdata is None
            assert trend_trace.customdata is None
            # The measured slice of August is still there, still clickable.
            assert any("2026-08" in t.x and t.customdata is not None for t in actual_traces)
            # Only the trend trace carries the legend when both are present.
            assert trend_trace.showlegend is not False
            assert remainder_trace.showlegend is False

    asyncio.run(_check())


def test_provider_and_home_pages_default_to_year_to_date(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The opening window is YTD, and both surfaces agree on it.

    Data runs Nov 2025 → Feb 2026, so the old rolling-6-month default (Sep 1, 2025) and YTD
    (Jan 1, 2026) are different answers — which is what makes this assertable. A rolling
    window also silently redraws every month, so the same page a week later wasn't the same
    window; a year anchor is fixed.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    _seed_daily_databricks(100, first=date(2025, 11, 1))

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("Jan 1 – Feb 8, 2026")
            await user.open("/")
            await user.should_see("Jan 1 – Feb 8, 2026")

    asyncio.run(_check())


def test_provider_page_explains_a_suppressed_trend_forecast(lake_home) -> None:  # type: ignore[no-untyped-def]
    """<3 complete months NULLs the trend rows; say why instead of drawing nothing."""
    from datetime import timedelta

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    rows = []
    for i in range(20):
        row = _rec(1)
        row.provider_name = ProviderName.GCP
        when = datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=i)
        row.charge_period_start = when
        row.charge_period_end = when
        rows.append(row)
    bronze.write_window(
        "t", IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), rows, ingest_run_id="r1"
    )
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/gcp")
            await user.should_see("Next 3 months")
            await user.should_see("3 complete months")

    asyncio.run(_check())


def test_commitment_panel_discloses_null_status_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Rows with no CommitmentDiscountStatus carry real dollars (and negative corrective
    lines). They're rightly off the Used/Unused chart, but dropping them from the
    denominator silently overstates how much of the commitment the split covers."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    rows = []
    for status, cost in (
        (CommitmentDiscountStatus.USED, "100"),
        (CommitmentDiscountStatus.UNUSED, "20"),
    ):
        row = _rec(15)
        row.service_name = "Amazon Redshift"
        row.commitment_discount_id = f"cud-{status}"
        row.commitment_discount_type = "Savings Plan"
        row.commitment_discount_category = CommitmentDiscountCategory.SPEND
        row.commitment_discount_status = status
        row.effective_cost = Decimal(cost)
        rows.append(row)
    # A commitment charge with no status, plus a negative correction — both real AWS
    # shapes (the real lake carries a −$41,284.75 NULL-status row). Distinct
    # commitment_discount_types so they stay separate rows in the GOLD aggregate:
    # summed into one group the negative would net away and become undetectable.
    for cost, kind in (("500", "Savings Plan"), ("-40", "Reservation")):
        row = _rec(16)
        row.service_name = "Amazon Redshift"
        row.commitment_discount_id = f"cud-nostatus-{kind}"
        row.commitment_discount_type = kind
        row.commitment_discount_status = None
        row.effective_cost = Decimal(cost)
        rows.append(row)
    bronze.write_window("t", window, rows, ingest_run_id="r1")
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            user.find(kind=ui.tab, content="Breakdown").click()
            await user.should_see("Commitment coverage")
            await user.should_see("no CommitmentDiscountStatus")
            await user.should_see("negative corrections")
            # 20 of 120 complete-month commitment spend is Unused.
            await user.should_see("16.7%")

    asyncio.run(_check())


def test_provider_nav_rows_are_bare_labels_with_databricks_first(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The "BY PROVIDER" nav rows read "Databricks" / "AWS Redshift", not
    "<label> spend" (the section heading already says these are spend pages), and
    Databricks sorts above the AWS/Redshift row even though discover_provider_groups()
    returns "aws" first alphabetically."""
    from flashlight.dashboard.router import _nav_groups, _nav_label
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    aws = _rec(15)
    # A Redshift service specifically: the "AWS Redshift" label is earned from what the
    # group actually holds (data._aws_label), not asserted by a static override, so a
    # group of EC2-only rows would honestly read "AWS".
    aws.service_name = "Amazon Redshift"
    dbx = _rec(15)
    dbx.provider_name = ProviderName.DATABRICKS
    dbx.service_name = "Databricks SQL"
    dbx.service_category = ServiceCategory.ANALYTICS
    bronze.write_window("t", window, [aws, dbx], ingest_run_id="r1")
    build_gold()

    assert _nav_groups() == ["databricks", "aws"]
    assert [_nav_label(group) for group in _nav_groups()] == ["Databricks", "AWS Redshift"]


def test_redshift_page_carries_the_shared_trend_panels(lake_home) -> None:  # type: ignore[no-untyped-def]
    """`/aws` renders the shared Trend & changes panels, not a lone monthly bar.

    These are the panels the page lacked while it was a fork of provider_focus: a daily
    series (which needed `service_name` on spend_trend_daily to exist at this scope at
    all) and the clickable month drill. If `/aws` ever stops going through
    provider_focus.render, this is what catches it.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window(
        "t",
        window,
        [_redshift_usage(15, "1000"), _redshift_usage(16, "500")],
        ingest_run_id="r1",
    )
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("Daily spend")
            await user.should_see("Monthly net cost")
            await user.should_not_see("click a bar to drill in")
            # The discount lives on the Net Spend card subtitle (net + savings = list is
            # arithmetic a reader doesn't need two tiles for); the list total survives as
            # the denominator, which is the part a bare percentage can't carry.
            await user.should_see("Net Spend")
            await user.should_see("savings vs. $1.5K list")
            await user.should_not_see("AWS Redshift list")
            await user.should_see("Alerts")
            # MoM prose used to sit under the KPIs; it lives on the Alerts tab now.
            await user.should_not_see("in the selected window")

    asyncio.run(_check())


def test_redshift_page_kpis_exclude_non_redshift_aws_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The narrowing must actually narrow — the headline is Redshift's, not the account's.

    This is the failure mode the scope exists to prevent: every shared panel reads a
    `<group>.<view>` that spans the whole AWS bill, so a missed predicate reports EC2 and
    S3 spend under a heading that says Redshift. Asserting on the *numbers* rather than
    on panel presence is the only way to catch that.
    """
    from flashlight.dashboard.views import provider_focus, redshift_focus
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    ec2 = _rec(15)  # service_name="AmazonEC2" — out of scope for this page
    ec2.effective_cost = ec2.billed_cost = Decimal("7000")
    ec2.list_cost = Decimal("7000")
    bronze.write_window("t", window, [_redshift_usage(15, "1000"), ec2], ingest_run_id="r1")
    build_gold()

    sm, end = date(2026, 5, 1), date(2026, 5, 31)
    scoped = provider_focus._bill_months(redshift_focus.scope(), sm, end)  # noqa: SLF001
    whole = provider_focus._bill_months(provider_focus.Scope("aws"), sm, end)  # noqa: SLF001

    assert float(scoped["net_cost"].sum()) == pytest.approx(1000.0), "Redshift only"
    assert float(whole["net_cost"].sum()) == pytest.approx(8000.0), "the whole AWS bill"
    # list_cost/savings must narrow with it — they come from the widened service view.
    assert float(scoped["list_cost"].sum()) == pytest.approx(1000.0)


def test_redshift_page_names_the_spend_it_hides(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Non-Redshift AWS spend is ingested but has no page — say so rather than let it
    silently vanish. Widening `include_services` in connections.yml is exactly how a user
    gets into this state."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    ec2 = _rec(15)
    ec2.effective_cost = ec2.billed_cost = Decimal("7000")
    bronze.write_window("t", window, [_redshift_usage(15, "1000"), ec2], ingest_run_id="r1")
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("of other AWS spend in this window")
            await user.should_see("AmazonEC2")

    asyncio.run(_check())


def test_out_of_scope_bill_does_not_read_as_a_broken_connection(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A connected account whose spend is all out of scope is NOT a missing connection.

    `/aws` with only EC2 spend has nothing to show, but telling the user to "enable the
    connection" is both wrong and alarming — the connection is working. The two empty
    states are different facts.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_rec(15)], ingest_run_id="r1")  # AmazonEC2 only
    build_gold()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("The account is connected and billing data is present")
            await user.should_not_see("may need to enable the connection")

    asyncio.run(_check())


def test_policy_tab_names_unevaluable_rows_instead_of_implying_compliance(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Redshift's real case: policy rows exist, but not one could be evaluated.

    `policy_record` is generated with no provider filter, and two of its rules key on
    entity_type='sql_warehouse' — which the Redshift connector emits. But Redshift reports
    neither warehouse tag counts nor auto-stop timeouts, so every row is not_applicable.
    Filtering by provider alone would render "— compliant · 0 non-compliant", which is
    indistinguishable from a clean bill of health. The tab has to say what happened.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    redshift_cost = _rec(15)
    redshift_cost.service_name = "Amazon Redshift"
    # A Redshift-shaped efficiency row: sql_warehouse entity under provider AWS, with
    # none of the config fields the policy rules test.
    warehouse = EfficiencyRecord(
        provider_name="AWS",
        charge_month=date(2026, 5, 1),
        entity_type=EntityType.SQL_WAREHOUSE,
        entity_id="prod-cluster",
        billed_cost=Decimal("500"),
        x_source_connector="redshift",
    )
    _seed([warehouse], cost_rows=[_db_cost_row(), redshift_cost])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("Policy Compliance")
            user.find(kind=ui.tab, content="Policy Compliance").click()
            await user.should_see("Nothing could be evaluated")
            await user.should_see("This is a telemetry coverage gap, not compliance")
            # The count that was invisible on every provider before.
            await user.should_see("Not evaluated")

    asyncio.run(_check())


def test_policy_tab_is_provider_filtered(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Each provider's tab shows only its own rows.

    `views/policy.py` read `SELECT * FROM policy.policy_record` unfiltered while it was a
    Databricks-only tab; as a core tab on every page, that would show Databricks' clusters
    under the AWS heading and vice versa.
    """
    from flashlight.dashboard.views import policy

    db_cluster = _eff("db-interactive", EntityType.INTERACTIVE, "100", auto_stop_minutes=0)
    warehouse = EfficiencyRecord(
        provider_name="AWS",
        charge_month=date(2026, 5, 1),
        entity_type=EntityType.SQL_WAREHOUSE,
        entity_id="prod-cluster",
        billed_cost=Decimal("500"),
        x_source_connector="redshift",
    )
    redshift_cost = _rec(15)
    redshift_cost.service_name = "Amazon Redshift"
    _seed([db_cluster, warehouse], cost_rows=[_db_cost_row(), redshift_cost])

    rows = policy._df("SELECT * FROM policy.policy_record")  # noqa: SLF001
    assert set(rows["provider_name"]) == {"Databricks", "AWS"}, "both providers land here"

    for provider, entity in (("Databricks", "db-interactive"), ("AWS", "prod-cluster")):
        scoped = rows[rows["provider_name"] == provider]
        assert set(scoped["entity_id"]) == {entity}, f"{provider} sees only its own entities"


def test_policy_tab_empty_state_names_the_provider(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A provider with no policy telemetry at all gets a named empty state, not a hidden
    tab — same discipline as Efficiency & Waste."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    gcp = _rec(15)
    gcp.provider_name = ProviderName.GCP
    _seed([], cost_rows=[gcp])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/gcp")
            await user.should_see("Policy Compliance")
            user.find(kind=ui.tab, content="Policy Compliance").click()
            await user.should_see("No policy signals for GCP")
            await user.should_see("coverage gap, not a clean bill of health")

    asyncio.run(_check())


# ── Databricks "AI Costs" tab ────────────────────────────────────────────────
# The tab is cost-only until the system.serving token plane lands, so these assert on the
# coverage/empty-state wording as hard as on the dollar figures: the whole point of the tab
# is that "we never measured tokens" can't be mistaken for "there are no tokens".
def _ai_cost_row(
    *,
    service_name: str = "MODEL_SERVING",
    cost: str = "500",
    resource_id: str = "ep-chat",
    resource_name: str = "chat-endpoint",
    tags: dict[str, str] | None = None,
) -> FocusRecord:
    """A Databricks AI-categorized cost row — what `gold.ai_spend_month` selects on."""
    return FocusRecord(
        provider_name=ProviderName.DATABRICKS,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=datetime(2026, 5, 15, tzinfo=UTC),
        charge_period_end=datetime(2026, 5, 15, 1, tzinfo=UTC),
        billed_cost=Decimal(cost),
        effective_cost=Decimal(cost),
        list_cost=Decimal(cost),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.AI_AND_MACHINE_LEARNING,
        service_name=service_name,
        resource_id=resource_id,
        resource_name=resource_name,
        resource_type="Model Serving Endpoint",
        sku_id="PREMIUM_SERVERLESS_REAL_TIME_INFERENCE",
        consumed_quantity=42.0,
        consumed_unit="DBU",
        tags=tags or {},
        x_source_connector="t",
    )


def test_ai_costs_tab_shows_spend_and_says_tokens_were_never_measured(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Cost renders from the bill; missing token telemetry is a KPI —, not a measured 0,
    and the token-detail panels stay omitted rather than repeating the gap three times.

    Panel titles follow the page date range — no pinned ``· May 2026`` / ``last N month(s)``.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed([], cost_rows=[_db_cost_row(), _ai_cost_row(tags={"project": "rag-search"})])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("AI Costs")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("AI Spend")
            await user.should_see("By AI product")
            await user.should_see("By resource")
            await user.should_see("AI Spend by product")
            await user.should_not_see("last 6 month")
            await user.should_not_see("By AI product ·")
            await user.should_not_see("By resource ·")
            await user.should_see("AI spend by tag")
            await user.should_see("no serving telemetry")
            await user.should_not_see("Tokens by project")
            await user.should_not_see("Model unit economics")

    asyncio.run(_check())


def test_ai_costs_tab_names_untagged_ai_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Untagged AI spend gets its own KPI, so a low tagging rate can't be skimmed past —
    it is the lever that makes per-project attribution work at all.

    Defaults the Values-for-one-key picker to ``project`` when that key is present.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed(
        [],
        cost_rows=[
            _db_cost_row(),
            _ai_cost_row(resource_id="ep-a", resource_name="tagged", tags={"project": "rag"}),
            _ai_cost_row(resource_id="ep-b", resource_name="untagged-endpoint"),
        ],
    )

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("Untagged AI Spend")
            await user.should_see("AI spend by tag")
            selects = list(user.find(kind=ui.select).elements)
            assert any(getattr(s, "value", None) == "project" for s in selects), (
                f"expected tag-key select defaulting to project, got "
                f"{[getattr(s, 'value', None) for s in selects]}"
            )

    asyncio.run(_check())


def test_ai_costs_tab_empty_state_distinguishes_no_ai_spend_from_no_measurement(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A Databricks bill with no AI rows is an absence of AI *spend* — a different fact
    from an absence of token measurement, so it gets its own wording."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed([], cost_rows=[_db_cost_row()])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("No AI spend found")
            await user.should_see("categorization comes from the billing data itself")

    asyncio.run(_check())


def test_ai_costs_tab_survives_a_lake_published_before_the_view_existed(lake_home) -> None:  # type: ignore[no-untyped-def]
    """An older lake has no ai_spend_month.parquet. The tab must say so and move on, not
    take the whole provider page down — the gold_view_published guard."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.lake import paths

    _seed([], cost_rows=[_db_cost_row(), _ai_cost_row()])
    (paths.gold_dir() / "databricks" / "ai_spend_month.parquet").unlink()

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("flashlight transform")
            # The rest of the page still works.
            await user.should_see("Trend & changes")

    asyncio.run(_check())


def _ai_usage_rec(**kw: object) -> Any:
    """An AiUsageRecord for the AI Costs tab's token panels."""
    from flashlight.lake.ai_usage_schema import AiUsageRecord

    base = {
        "provider_name": "Databricks",
        "charge_month": date(2026, 5, 1),
        "endpoint_id": "ep-chat",
        "endpoint_name": "chat-endpoint",
        "served_entity_id": "se-1",
        "model_name": "llama-3-70b",
        "model_kind": "FOUNDATION_MODEL",
        "serving_mode": "pay_per_token",
        "requester": "alice@example.com",
        "request_count": 10,
        "input_tokens": 800_000,
        "output_tokens": 200_000,
        "x_source_connector": "databricks",
    }
    return AiUsageRecord(**{**base, **kw})


def _seed_ai(usage: list[Any], cost_rows: list[FocusRecord]) -> None:
    from flashlight.lake import bronze
    from flashlight.lake.ai_usage import write_ai_usage
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, cost_rows, ingest_run_id="r1")
    write_ai_usage(window, usage)
    build_gold()


def test_ai_spend_kpi_is_a_slice_of_databricks_net_not_an_addition(lake_home) -> None:  # type: ignore[no-untyped-def]
    """AI spend gets a card on the provider KPI row, and it is part of the net figure
    beside it — same bill, same dollars.

    The seed is $100 of SQL plus $500 of Model Serving: net reads $600, the AI card $500,
    and $1,100 must appear nowhere. That is the opposite contract to the backing-storage
    card next to it (AWS-billed, outside net), which is why the two carry different
    sub-lines and different hues.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views.ai_costs import KPI_SUB

    _seed_ai([_ai_usage_rec()], [_db_cost_row(), _ai_cost_row()])

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            # On the landing KPI row, before the AI Costs tab is opened.
            await user.should_see("AI Spend")
            await user.should_see("$500")
            await user.should_see(KPI_SUB)
            await user.should_see("$600")
            await user.should_not_see("$1,100")

    asyncio.run(_check())


def test_ai_costs_tab_shows_tokens_per_project_and_per_user(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Token telemetry surfaces per user and model; bill tags use Values-for-one-key.

    Asserted on panel titles and captions — ui.table rows are a data prop should_see
    can't reach (row-level correctness is pinned in test_ai_usage_views).
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed_ai(
        [
            _ai_usage_rec(requester="alice@example.com", input_tokens=800_000),
            _ai_usage_rec(requester="svc-rag", input_tokens=400_000),
        ],
        [_db_cost_row(), _ai_cost_row(tags={"project": "rag-search"})],
    )

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("AI spend by tag")
            await user.should_see("Tokens by user")
            await user.should_see("Model unit economics")
            await user.should_not_see("Tokens by project")
            await user.should_not_see("Blank cost = not token-metered")
            await user.should_not_see("Service principals listed separately")
            await user.should_not_see("Compare $/1M within one serving mode")

    asyncio.run(_check())


def test_ai_costs_tab_names_cost_that_cannot_be_split_by_tokens(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Provisioned endpoints still surface on Tokens by user with a blank allocated cost —
    never coalesced to $0. Row-level blankness is pinned in test_ai_usage_views."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed_ai(
        [_ai_usage_rec(serving_mode="provisioned_throughput", input_tokens=1_000_000)],
        [_db_cost_row(), _ai_cost_row()],
    )

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("Tokens by user")
            await user.should_not_see("Blank cost = not token-metered")

    asyncio.run(_check())


def test_ai_costs_tab_omits_token_panels_when_serving_pull_is_empty(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Cost-only FULL OUTER rows must not invent a measured-zero token story.

    ``endpoint_month`` can be non-empty from the bill side alone; Tokens still reads —,
    and the user/model panels stay off the page. Bill tag values still render.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed_ai([], [_db_cost_row(), _ai_cost_row(tags={"project": "rag"})])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("AI Spend")
            await user.should_see("no serving telemetry")
            await user.should_see("AI spend by tag")
            await user.should_not_see("Tokens by project")
            await user.should_not_see("Tokens by user")
            await user.should_not_see("What can be optimized")

    asyncio.run(_check())


def test_ai_costs_tab_includes_genie(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Genie bills as warehouse-shaped usage, so the vendored FOCUS query files it under a
    non-AI service_category. It must still show up as AI spend."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    genie = _ai_cost_row(service_name="AI_BI_GENIE", resource_id="genie-1", cost="777")
    genie.service_category = ServiceCategory.DATABASES
    _seed_ai([], [_db_cost_row(), genie])

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("By AI product")
            # ui.table rows are a client-side data prop should_see can't reach, so the
            # product label is asserted against the table's own rows.
            rows = " ".join(str(t.rows) for t in user.find(kind=ui.table).elements)
            assert "AI/BI Genie" in rows, "Genie spend is missing from the AI product table"

    asyncio.run(_check())


def test_ai_costs_tab_shows_one_row_per_project_not_one_per_attribution_source(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Bill-tag Values-for-one-key collapses spend to one row per tag value.

    Serving-side project_source folding (usage_context vs endpoint tag) stays a GOLD
    concern — pinned in test_ai_usage_views — not a dashboard Tokens-by-project table.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    _seed_ai(
        [
            _ai_usage_rec(requester="alice@example.com", usage_context_project="rag-search"),
            _ai_usage_rec(requester="bob@example.com"),
        ],
        [
            _db_cost_row(),
            _ai_cost_row(resource_id="ep-chat", cost="100", tags={"project": "rag-search"}),
            _ai_cost_row(resource_id="ep-other", cost="50", tags={"project": "rag-search"}),
        ],
    )

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="AI Costs").click()
            await user.should_see("AI spend by tag")
            tables = [
                t.rows
                for t in user.find(kind=ui.table).elements
                if {"project", "Net cost"} <= {str(c.get("name")) for c in (t.columns or [])}
            ]
            assert tables, "the by-tag table was not found"
            # searchable_table can expose more than one ui.table element; assert on the first.
            project_rows = [r for r in tables[0] if r.get("project") == "rag-search"]
            assert len(project_rows) == 1, f"expected one rag-search row, got {project_rows}"
            assert project_rows[0]["Net cost"] == "$150"

    asyncio.run(_check())


# ── Backing storage tab (AWS-billed S3 behind Databricks) ─────────────────────
def _s3_usage(day: int, amount: str, resource_id: str | None) -> FocusRecord:
    rec = _rec(day)
    rec.service_name = "Amazon Simple Storage Service"
    rec.service_category = ServiceCategory.STORAGE
    rec.resource_id = resource_id
    rec.effective_cost = Decimal(amount)
    rec.billed_cost = Decimal(amount)
    rec.list_cost = Decimal(amount)
    return rec


def _seed_backing_storage() -> None:
    from flashlight.lake import bronze
    from flashlight.lake.storage_location_schema import StorageLocationRecord
    from flashlight.lake.storage_locations import write_storage_locations
    from flashlight.transform.runner import build_gold

    dbx = _rec(15)
    dbx.provider_name = ProviderName.DATABRICKS
    dbx.service_name = "JOBS"
    dbx.service_category = ServiceCategory.ANALYTICS

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window(
        "t",
        window,
        [
            _redshift_usage(14, "500"),
            _s3_usage(15, "300", "arn:aws:s3:::acme-uc-root"),
            _s3_usage(16, "40", "arn:aws:s3:::random-other"),
            # Deliberately the priciest managed bucket, so "metastore root first" is a
            # real ordering assertion and not cost order in disguise.
            _s3_usage(17, "900", "arn:aws:s3:::acme-brz"),
            dbx,
        ],
        ingest_run_id="r1",
    )
    write_storage_locations(
        [
            StorageLocationRecord(
                provider_name="Databricks",
                snapshot_month=date(2026, 5, 1),
                location_kind="metastore_root",
                location_name="acme",
                url="s3://acme-uc-root",
                scheme="s3",
                cloud_provider_name="AWS",
                bucket_name="acme-uc-root",
                key_prefix=None,
                x_source_connector="databricks",
            ),
            StorageLocationRecord(
                provider_name="Databricks",
                snapshot_month=date(2026, 5, 1),
                location_kind="catalog",
                location_name="bronze_catalog",
                url="s3://acme-brz",
                scheme="s3",
                cloud_provider_name="AWS",
                bucket_name="acme-brz",
                key_prefix=None,
                x_source_connector="databricks",
            ),
        ]
    )
    build_gold()


def test_backing_storage_tab_lists_each_managed_bucket_with_its_owner_once(lake_home) -> None:  # type: ignore[no-untyped-def]
    """One table, metastore roots first, every cost a bare dollar figure.

    The bucket list and the per-catalog list were separate panels rendering the same rows
    twice (each managed object sits on its own bucket), so they are one table now — which
    is what makes `should_not_see` on the old panel title the de-duplication assertion.
    Costs are plain ``$…`` (no ``≤`` prefix, no jargon ``Basis`` column).
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    _seed_backing_storage()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("Databricks Storage")
            user.find(kind=ui.tab, content="Databricks Storage").click()
            await user.should_see("Databricks Storage (billed by AWS)")
            await user.should_see("Databricks-managed storage")
            # The second, duplicate panel is gone.
            await user.should_not_see("Databricks-managed buckets")
            # ...and so is the per-bucket list of everything unmanaged: with thousands of
            # unrelated buckets on a real account it buried the one number that matters.
            await user.should_not_see("S3 buckets Unity Catalog does not point at")

            tables = [
                t
                for t in user.find(kind=ui.table).elements
                if {"Bucket", "Catalog / metastore"}
                <= {str(c.get("name")) for c in (t.columns or [])}
            ]
            assert len(tables) == 1, f"expected exactly one managed-storage table, got {tables}"
            table = tables[0]
            rows = table.rows
            cols = {str(c.get("name")) for c in (table.columns or [])}
            assert [r["Kind"] for r in rows] == ["Metastore root", "Catalog"], (
                f"metastore roots must be listed before catalogs, got {rows}"
            )
            assert rows[0]["Bucket"] == "acme-uc-root"
            assert rows[0]["Catalog / metastore"] == "acme"
            assert rows[1]["Catalog / metastore"] == "bronze_catalog"
            assert "Basis" not in cols, f"jargon Basis column must be gone: {cols}"
            costs = [r["S3 cost (AWS-billed)"] for r in rows]
            assert all(c.startswith("$") and "≤" not in c for c in costs), (
                f"costs must be bare $… with no ≤: {costs}"
            )

    asyncio.run(_check())


def test_backing_storage_kpi_sits_beside_databricks_net_without_entering_it(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The AWS-billed storage cost is a card on the Databricks KPI row — and the
    ``Net Spend`` card beside it is unmoved by that.

    Both halves matter. The card is there because "what does Databricks cost me?" is asked
    at the top of the page, not a tab away. The net figure stays DBU-only because adding
    the two is the removed TCO join (CLAUDE.md, "No cross-provider cost join") — so the
    seed makes storage ($1.2K over two managed buckets) two orders of magnitude larger
    than the Databricks bill ($10), which means a sum could not hide in rounding.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    _seed_backing_storage()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            # On the landing KPI row, before any tab is clicked.
            await user.should_see("Databricks Storage")
            await user.should_see("$1.2K")
            # $10 of DBU compute, unchanged — not $1,210.
            await user.should_see("$10")
            await user.should_not_see("$1,210")

    asyncio.run(_check())


def test_backing_storage_kpi_is_absent_rather_than_zero_when_nothing_is_mapped(lake_home) -> None:  # type: ignore[no-untyped-def]
    """No map means unmeasured, and unmeasured must never render as "$0" storage cost.

    A zero card on the KPI row would answer "what does Databricks storage cost?" with
    "nothing" on exactly the lake that has not looked yet, so the card is omitted and the
    tab's empty state says which cause it is.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    dbx = _rec(15)
    dbx.provider_name = ProviderName.DATABRICKS
    dbx.service_name = "JOBS"
    dbx.service_category = ServiceCategory.ANALYTICS
    bronze.write_window(
        "t",
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)),
        [dbx, _s3_usage(15, "300", "arn:aws:s3:::acme-uc-root")],
        ingest_run_id="r1",
    )
    build_gold()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            await user.should_see("Net Spend")
            # Tab label still exists; the KPI card must not — unmeasured is omitted,
            # never rendered as a "$0" storage cost beside Net Spend.
            from flashlight.dashboard.views import backing_storage

            assert backing_storage.kpi_card(date(2026, 5, 1), date(2026, 5, 31)) is None
            await user.should_see("Databricks Storage")  # tab label still present

    asyncio.run(_check())


def test_backing_storage_tab_is_not_on_the_aws_page(lake_home) -> None:  # type: ignore[no-untyped-def]
    """It belongs to the platform whose storage it describes, not to the biller."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    _seed_backing_storage()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_not_see("Databricks Storage (billed by AWS)")

    asyncio.run(_check())


def test_backing_storage_tab_says_empty_map_is_not_zero_storage_cost(lake_home) -> None:  # type: ignore[no-untyped-def]
    """S3 spend present, nothing identified as Databricks-managed — the case most easily
    misread.

    A token that cannot read the metastore summary produces exactly this, and the tab must
    not let it read as "Databricks has no storage cost" ("never measured" must never look
    like "nothing to find"). The total S3 denominator is still stated so the reader knows
    how much spend was examined.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views.backing_storage import NO_LOCATIONS_MSG
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    dbx = _rec(15)
    dbx.provider_name = ProviderName.DATABRICKS
    dbx.service_name = "JOBS"
    dbx.service_category = ServiceCategory.ANALYTICS
    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    # S3 cost, but write_storage_locations is never called → no map.
    bronze.write_window(
        "t", window, [dbx, _s3_usage(15, "300", "arn:aws:s3:::acme-uc-root")], ingest_run_id="r1"
    )
    build_gold()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/databricks")
            user.find(kind=ui.tab, content="Databricks Storage").click()
            await user.should_see(NO_LOCATIONS_MSG)
            # ...and the denominator is still stated, as one line rather than a table of
            # every unrelated bucket.
            await user.should_see("none of it currently identified as Databricks-managed")

    asyncio.run(_check())


def test_redshift_page_points_hidden_s3_spend_at_the_backing_storage_tab(lake_home) -> None:  # type: ignore[no-untyped-def]
    """`/aws` is Redshift-scoped; S3 is ingested for the storage plane but kept out of
    ``aws.*`` GOLD. The scope caption still points at Databricks → Databricks Storage
    when the storage plane has dollars for the window.
    """
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window(
        "t",
        window,
        [_redshift_usage(14, "500"), _s3_usage(15, "300", "arn:aws:s3:::acme-uc-root")],
        ingest_run_id="r1",
    )
    build_gold()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("Redshift's own FOCUS service names")
            await user.should_see("Databricks \u2192 Databricks Storage")
            await user.should_see("not in aws.* GOLD")

    asyncio.run(_check())


def test_home_folds_databricks_storage_from_storage_plane(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Home adds mapped Databricks Storage onto Databricks; aws.* GOLD has no S3.

    Bronze still holds Amazon S3. Transform keeps it out of ``aws.*`` and names mapped
    buckets ``Databricks Storage`` in ``storage.backing_storage_month``. Home folds that
    into the Databricks stack / movers — it does not rename an AWS S3 service line.
    ``databricks.monthly_bill`` stays DBU-only.
    """
    from flashlight.lake import bronze
    from flashlight.lake.storage_location_schema import StorageLocationRecord
    from flashlight.lake.storage_locations import write_storage_locations
    from flashlight.transform.runner import build_gold

    def _on(day: int, month: int) -> datetime:
        return datetime(2026, month, day, tzinfo=UTC)

    def _dbx(month: int, amount: str = "100") -> FocusRecord:
        when = _on(15, month)
        last = 31 if month == 5 else 30
        return FocusRecord(
            provider_name=ProviderName.DATABRICKS,
            billing_account_id="acct",
            billing_period_start=date(2026, month, 1),
            billing_period_end=date(2026, month, last),
            charge_period_start=when,
            charge_period_end=when,
            billed_cost=Decimal(amount),
            effective_cost=Decimal(amount),
            list_cost=Decimal(amount),
            charge_category=ChargeCategory.USAGE,
            service_category=ServiceCategory.ANALYTICS,
            service_name="JOBS",
            tags={},
            x_compute_class=ComputeClass.NOT_APPLICABLE,
            x_source_connector="t",
        )

    def _aws_month(month: int, *, rs: str, managed: str, unmapped: str) -> list[FocusRecord]:
        last = 31 if month == 5 else 30

        def stamp(rec: FocusRecord, day: int) -> FocusRecord:
            rec.billing_period_start = date(2026, month, 1)
            rec.billing_period_end = date(2026, month, last)
            rec.charge_period_start = _on(day, month)
            rec.charge_period_end = _on(day, month)
            return rec

        return [
            stamp(_redshift_usage(14, rs), 14),
            stamp(_s3_usage(15, managed, "arn:aws:s3:::acme-uc-root"), 15),
            stamp(_s3_usage(16, unmapped, "arn:aws:s3:::random-other"), 16),
            _dbx(month),
        ]

    bronze.write_window(
        "t",
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)),
        _aws_month(5, rs="1000", managed="200", unmapped="50"),
        ingest_run_id="r1",
    )
    bronze.write_window(
        "t",
        IngestWindow(date(2026, 6, 1), date(2026, 6, 30)),
        _aws_month(6, rs="1000", managed="300", unmapped="50"),
        ingest_run_id="r2",
    )
    write_storage_locations(
        [
            StorageLocationRecord(
                provider_name="Databricks",
                snapshot_month=date(2026, 6, 1),
                location_kind="metastore_root",
                location_name="acme",
                url="s3://acme-uc-root",
                scheme="s3",
                cloud_provider_name="AWS",
                bucket_name="acme-uc-root",
                key_prefix=None,
                x_source_connector="databricks",
            ),
        ]
    )
    build_gold()

    from flashlight.dashboard.views import home_overview
    from flashlight.gold.reader import run_select

    dbx_bill = float(
        run_select(
            "SELECT sum(gross_cost) AS c FROM databricks.monthly_bill "
            "WHERE charge_month = '2026-06-01'"
        )[0]["c"]
    )
    assert dbx_bill == pytest.approx(100.0)
    aws_bill = float(
        run_select(
            "SELECT sum(gross_cost) AS c FROM aws.monthly_bill "
            "WHERE charge_month = '2026-06-01'"
        )[0]["c"]
    )
    # Redshift only — S3 excluded from aws.* GOLD.
    assert aws_bill == pytest.approx(1000.0)
    s3_in_aws = float(
        run_select(
            "SELECT coalesce(sum(gross_cost), 0) AS c FROM aws.spend_by_service_month "
            "WHERE service_name = 'Amazon Simple Storage Service'"
        )[0]["c"]
    )
    assert s3_in_aws == 0.0

    month, prior = date(2026, 6, 1), date(2026, 5, 1)
    storage = home_overview._databricks_storage_by_month(prior, month)
    assert storage[month] == pytest.approx(300.0)
    assert storage[prior] == pytest.approx(200.0)

    aws_cur, aws_prev = home_overview._include_storage(
        "aws", 1000.0, 1000.0, storage[month], storage[prior]
    )
    dbx_cur, dbx_prev = home_overview._include_storage(
        "databricks", 100.0, 100.0, storage[month], storage[prior]
    )
    assert aws_cur == pytest.approx(1000.0)
    assert aws_prev == pytest.approx(1000.0)
    assert dbx_cur == pytest.approx(400.0)
    assert dbx_prev == pytest.approx(300.0)

    movers = home_overview._home_movers(month, prior)
    assert not movers.empty
    drivers = list(movers["driver"])
    assert "Databricks Storage" in drivers
    assert "Amazon Simple Storage Service" not in drivers
    dbx_row = movers.loc[movers["driver"] == "Databricks Storage"].iloc[0]
    assert dbx_row["provider"] == "Databricks"
    assert float(dbx_row["cost_delta"]) == pytest.approx(100.0)

    history = home_overview._provider_history(["aws", "databricks"], prior, date(2026, 6, 30))
    jun_rows = history[history["month"] == "2026-06"]
    by_group = {str(r.group): float(r.net_cost) for r in jun_rows.itertuples()}
    assert by_group["aws"] == pytest.approx(1000.0)
    assert by_group["databricks"] == pytest.approx(400.0)

    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/")
            await user.should_see("Databricks includes its managed storage")
            tables = [
                t
                for t in user.find(kind=ui.table).elements
                if {"Provider", "Driver"} <= {str(c.get("name")) for c in (t.columns or [])}
            ]
            assert tables, "Biggest movers table missing"
            rows = tables[0].rows
            drivers = [r.get("Driver") for r in rows]
            assert "Databricks Storage" in drivers
            assert "Amazon Simple Storage Service" not in drivers

    asyncio.run(_check())


def test_home_folds_databricks_compute_from_compute_plane(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Home adds mapped Databricks Compute onto Databricks; aws.* GOLD has no EC2.

    The identical shape as ``test_home_folds_databricks_storage_from_storage_plane``,
    for EC2/backing-compute instead of S3/backing-storage — Home folds mapped EC2 into
    the Databricks stack / movers, never renames an AWS EC2 service line.
    ``databricks.monthly_bill`` stays DBU-only.
    """
    from flashlight.lake import bronze
    from flashlight.lake.compute_instance_schema import ComputeInstanceRecord
    from flashlight.lake.compute_instances import write_compute_instances
    from flashlight.transform.runner import build_gold

    def _on(day: int, month: int) -> datetime:
        return datetime(2026, month, day, tzinfo=UTC)

    def _dbx(month: int, amount: str = "100") -> FocusRecord:
        when = _on(15, month)
        last = 31 if month == 5 else 30
        return FocusRecord(
            provider_name=ProviderName.DATABRICKS,
            billing_account_id="acct",
            billing_period_start=date(2026, month, 1),
            billing_period_end=date(2026, month, last),
            charge_period_start=when,
            charge_period_end=when,
            billed_cost=Decimal(amount),
            effective_cost=Decimal(amount),
            list_cost=Decimal(amount),
            charge_category=ChargeCategory.USAGE,
            service_category=ServiceCategory.ANALYTICS,
            service_name="JOBS",
            tags={},
            x_compute_class=ComputeClass.NOT_APPLICABLE,
            x_source_connector="t",
        )

    def _ec2_usage(day: int, month: int, amount: str, resource_id: str | None) -> FocusRecord:
        last = 31 if month == 5 else 30
        when = _on(day, month)
        return FocusRecord(
            provider_name=ProviderName.AWS,
            billing_account_id="acct",
            billing_period_start=date(2026, month, 1),
            billing_period_end=date(2026, month, last),
            charge_period_start=when,
            charge_period_end=when,
            billed_cost=Decimal(amount),
            effective_cost=Decimal(amount),
            list_cost=Decimal(amount),
            charge_category=ChargeCategory.USAGE,
            service_category=ServiceCategory.COMPUTE,
            service_name="Amazon Elastic Compute Cloud",
            resource_id=resource_id,
            tags={},
            x_compute_class=ComputeClass.NOT_APPLICABLE,
            x_source_connector="t",
        )

    def _aws_month(month: int, *, rs: str, managed: str, unmapped: str) -> list[FocusRecord]:
        last = 31 if month == 5 else 30

        def stamp(rec: FocusRecord, day: int) -> FocusRecord:
            rec.billing_period_start = date(2026, month, 1)
            rec.billing_period_end = date(2026, month, last)
            rec.charge_period_start = _on(day, month)
            rec.charge_period_end = _on(day, month)
            return rec

        return [
            stamp(_redshift_usage(14, rs), 14),
            _ec2_usage(15, month, managed, "i-managed1"),
            _ec2_usage(16, month, unmapped, "i-unmapped9"),
            _dbx(month),
        ]

    bronze.write_window(
        "t",
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)),
        _aws_month(5, rs="1000", managed="200", unmapped="50"),
        ingest_run_id="r1",
    )
    bronze.write_window(
        "t",
        IngestWindow(date(2026, 6, 1), date(2026, 6, 30)),
        _aws_month(6, rs="1000", managed="300", unmapped="50"),
        ingest_run_id="r2",
    )
    write_compute_instances(
        IngestWindow(date(2026, 5, 1), date(2026, 6, 30)),
        [
            ComputeInstanceRecord(
                provider_name="Databricks",
                charge_month=date(2026, 5, 1),
                cluster_id="c1",
                instance_id="i-managed1",
                is_driver=True,
                x_source_connector="databricks",
            ),
            ComputeInstanceRecord(
                provider_name="Databricks",
                charge_month=date(2026, 6, 1),
                cluster_id="c1",
                instance_id="i-managed1",
                is_driver=True,
                x_source_connector="databricks",
            ),
        ],
    )
    build_gold()

    from flashlight.dashboard.views import home_overview
    from flashlight.gold.reader import run_select

    dbx_bill = float(
        run_select(
            "SELECT sum(gross_cost) AS c FROM databricks.monthly_bill "
            "WHERE charge_month = '2026-06-01'"
        )[0]["c"]
    )
    assert dbx_bill == pytest.approx(100.0)
    aws_bill = float(
        run_select(
            "SELECT sum(gross_cost) AS c FROM aws.monthly_bill "
            "WHERE charge_month = '2026-06-01'"
        )[0]["c"]
    )
    # Redshift only — EC2 excluded from aws.* GOLD.
    assert aws_bill == pytest.approx(1000.0)
    ec2_in_aws = float(
        run_select(
            "SELECT coalesce(sum(gross_cost), 0) AS c FROM aws.spend_by_service_month "
            "WHERE service_name = 'Amazon Elastic Compute Cloud'"
        )[0]["c"]
    )
    assert ec2_in_aws == 0.0

    month, prior = date(2026, 6, 1), date(2026, 5, 1)
    compute = home_overview._databricks_compute_by_month(prior, month)
    assert compute[month] == pytest.approx(300.0)
    assert compute[prior] == pytest.approx(200.0)

    aws_cur, aws_prev = home_overview._include_storage(
        "aws", 1000.0, 1000.0, compute[month], compute[prior]
    )
    dbx_cur, dbx_prev = home_overview._include_storage(
        "databricks", 100.0, 100.0, compute[month], compute[prior]
    )
    assert aws_cur == pytest.approx(1000.0)
    assert aws_prev == pytest.approx(1000.0)
    assert dbx_cur == pytest.approx(400.0)
    assert dbx_prev == pytest.approx(300.0)

    movers = home_overview._home_movers(month, prior)
    assert not movers.empty
    drivers = list(movers["driver"])
    assert "Databricks Compute" in drivers
    assert "Amazon Elastic Compute Cloud" not in drivers
    dbx_row = movers.loc[movers["driver"] == "Databricks Compute"].iloc[0]
    assert dbx_row["provider"] == "Databricks"
    assert float(dbx_row["cost_delta"]) == pytest.approx(100.0)

    history = home_overview._provider_history(["aws", "databricks"], prior, date(2026, 6, 30))
    jun_rows = history[history["month"] == "2026-06"]
    by_group = {str(r.group): float(r.net_cost) for r in jun_rows.itertuples()}
    assert by_group["aws"] == pytest.approx(1000.0)
    assert by_group["databricks"] == pytest.approx(400.0)


# ── Total Databricks footprint card (databricks_footprint.py) ────────────────────
def test_footprint_card_omitted_when_no_backing_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A Databricks-only lake (no S3/EC2 mapped anywhere) must not get a footprint
    card identical to Net Spend — that would just be visual noise beside it."""
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)),
        [_dbx_rec(5, "100")],
        ingest_run_id="r1",
    )
    build_gold()

    from flashlight.dashboard.views.databricks_footprint import footprint_card

    assert footprint_card(date(2026, 5, 1), date(2026, 5, 31)) is None


def _dbx_rec(day: int, amount: str) -> FocusRecord:
    when = datetime(2026, 5, day, tzinfo=UTC)
    return FocusRecord(
        provider_name=ProviderName.DATABRICKS,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=when,
        charge_period_end=when,
        billed_cost=Decimal(amount),
        effective_cost=Decimal(amount),
        list_cost=Decimal(amount),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.ANALYTICS,
        service_name="JOBS",
        tags={},
        x_compute_class=ComputeClass.NOT_APPLICABLE,
        x_source_connector="t",
    )


def test_footprint_card_combines_dbu_storage_and_compute(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Net Spend (DBU) + Backing storage + Backing compute, in one clearly-labelled
    card — and ``databricks.monthly_bill`` itself is untouched by any of it."""
    from flashlight.lake import bronze
    from flashlight.lake.compute_instance_schema import ComputeInstanceRecord
    from flashlight.lake.compute_instances import write_compute_instances
    from flashlight.lake.storage_location_schema import StorageLocationRecord
    from flashlight.lake.storage_locations import write_storage_locations
    from flashlight.transform.runner import build_gold

    def _s3(day: int, amount: str) -> FocusRecord:
        rec = _dbx_rec(day, "0")
        rec.provider_name = ProviderName.AWS
        rec.service_name = "Amazon Simple Storage Service"
        rec.service_category = ServiceCategory.STORAGE
        rec.resource_id = "arn:aws:s3:::acme-uc-root"
        rec.effective_cost = rec.billed_cost = rec.list_cost = Decimal(amount)
        return rec

    def _ec2(day: int, amount: str) -> FocusRecord:
        rec = _dbx_rec(day, "0")
        rec.provider_name = ProviderName.AWS
        rec.service_name = "Amazon Elastic Compute Cloud"
        rec.service_category = ServiceCategory.COMPUTE
        rec.resource_id = "i-managed1"
        rec.effective_cost = rec.billed_cost = rec.list_cost = Decimal(amount)
        return rec

    bronze.write_window(
        "t",
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)),
        [_dbx_rec(15, "100"), _s3(16, "40"), _ec2(17, "60")],
        ingest_run_id="r1",
    )
    write_storage_locations(
        [
            StorageLocationRecord(
                provider_name="Databricks",
                snapshot_month=date(2026, 5, 1),
                location_kind="metastore_root",
                location_name="acme",
                url="s3://acme-uc-root",
                scheme="s3",
                cloud_provider_name="AWS",
                bucket_name="acme-uc-root",
                key_prefix=None,
                x_source_connector="databricks",
            ),
        ]
    )
    write_compute_instances(
        IngestWindow(date(2026, 5, 1), date(2026, 5, 31)),
        [
            ComputeInstanceRecord(
                provider_name="Databricks",
                charge_month=date(2026, 5, 1),
                cluster_id="c1",
                instance_id="i-managed1",
                is_driver=True,
                x_source_connector="databricks",
            ),
        ],
    )
    build_gold()

    from flashlight.dashboard.views.databricks_footprint import footprint_card
    from flashlight.gold.reader import run_select

    card = footprint_card(date(2026, 5, 1), date(2026, 5, 31))
    assert card is not None
    title, value, sub = card[0], card[1], card[2]
    assert title == "Total cost of ownership"
    assert value == "$200"  # 100 DBU + 40 storage + 60 compute
    assert sub == "Includes Databricks usage and AWS infrastructure"

    # Net Spend itself must be exactly the DBU figure, untouched.
    dbu = float(
        run_select(
            "SELECT sum(net_cost) AS c FROM databricks.monthly_bill "
            "WHERE charge_month = '2026-05-01'"
        )[0]["c"]
    )
    assert dbu == pytest.approx(100.0)

    # Trend & changes' stacked bar gets the identical two extra segments, shaped so they
    # concatenate straight onto _monthly_by_service's own output.
    from flashlight.dashboard.views.provider_focus import _databricks_backing_monthly

    backing = _databricks_backing_monthly(date(2026, 5, 31), date(2026, 5, 1))
    by_service = {
        str(r.service_name): float(r.net_cost) for r in backing.itertuples(index=False)
    }
    assert by_service == {"Databricks Storage": 40.0, "Databricks Compute": 60.0}


# ── gold_session() — request-scoped connection reuse ──────────────────────────
# "Speed up dashboard page loads": gold_df() re-registered the whole GOLD lake on
# every single call (~50 times for one /databricks render — see chrome.lazy_tab_panels'
# own docstring for the measured split). gold_session() scopes one already-registered
# connection to a page render instead, with none of a process-wide cache's cross-request
# lock contention (see data.py's module docstring for why that alternative was rejected).


def test_gold_session_reuses_one_connection_across_gold_df_calls(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Every gold_df() call inside one gold_session() shares its connection — one
    duck.connect()/register_gold() pair for the whole block, not one per call.
    Outside any session, gold_df() is unchanged: one connection per call."""
    from flashlight.dashboard import data
    from flashlight.lake import bronze, duck
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t", IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), [_rec(15)], ingest_run_id="r1"
    )
    build_gold()

    calls = {"connect": 0, "register": 0}
    real_connect = duck.connect
    real_register = duck.register_gold

    def counting_connect():  # type: ignore[no-untyped-def]
        calls["connect"] += 1
        return real_connect()

    def counting_register(con):  # type: ignore[no-untyped-def]
        calls["register"] += 1
        real_register(con)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(duck, "connect", counting_connect)
        mp.setattr(duck, "register_gold", counting_register)

        with data.gold_session():
            for _ in range(3):
                data.gold_df('SELECT * FROM "aws".monthly_bill')
        assert calls == {"connect": 1, "register": 1}, "one connection for the whole session"

        data.gold_df('SELECT * FROM "aws".monthly_bill')
        data.gold_df('SELECT * FROM "aws".monthly_bill')
        assert calls == {"connect": 3, "register": 3}, (
            "outside a session, gold_df() still opens/registers per call"
        )


def test_gold_session_nested_call_reuses_the_outer_connection(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A gold_session() entered while another is already active (e.g. one page's
    render calling into another page's helper) reuses the outer connection — it
    must not open a second one and close it out from under the outer block."""
    from flashlight.dashboard import data
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t", IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), [_rec(15)], ingest_run_id="r1"
    )
    build_gold()

    with data.gold_session():
        outer = data._session_con.get()  # noqa: SLF001
        assert outer is not None
        with data.gold_session():
            assert data._session_con.get() is outer, "nested session reuses the outer connection"  # noqa: SLF001
            data.gold_df('SELECT * FROM "aws".monthly_bill')
        # The inner block's exit must not have closed the connection the outer
        # block still owns.
        data.gold_df('SELECT * FROM "aws".monthly_bill')
    assert data._session_con.get() is None  # noqa: SLF001


def test_gold_sessions_are_isolated_across_concurrent_threads(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Two page renders dispatched to different threads (Starlette's own dispatch
    for sync page handlers) must never see each other's connection — the property
    that lets gold_session() skip the lock a process-wide cached connection would
    need."""
    import threading

    from flashlight.dashboard import data
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t", IngestWindow(date(2026, 5, 1), date(2026, 5, 31)), [_rec(15)], ingest_run_id="r1"
    )
    build_gold()

    seen: dict[str, object] = {}
    barrier = threading.Barrier(2, timeout=5)
    errors: list[BaseException] = []

    def render(name: str) -> None:
        try:
            with data.gold_session():
                seen[name] = data._session_con.get()  # noqa: SLF001
                barrier.wait()  # hold both sessions open at once, overlapping in time
                data.gold_df('SELECT * FROM "aws".monthly_bill')
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(exc)

    threads = [threading.Thread(target=render, args=(name,)) for name in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert seen["a"] is not None and seen["b"] is not None
    assert seen["a"] is not seen["b"], "each thread's session must get its own connection"
