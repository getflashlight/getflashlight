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
            bronze.write_window("t", window, [_rec(31)], ingest_run_id="r1")
            build_gold()

            await user.open("/aws")
            await user.should_see("Redshift spend")

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

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/aws")
            await user.should_see("Commitment coverage")

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
        run_id="sync-1", connector="aws_focus", status="success", rows=10,
        started_at=_ts(0), finished_at=_ts(1),
    )
    runlog.record_run(
        run_id="sync-2", connector="databricks", status="failed", rows=0,
        started_at=_ts(2), finished_at=_ts(3), detail="expired token",
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


def test_chat_page_sends_a_question_and_renders_the_reply(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """BYOK chat page: typing a question and clicking Send wires through to the
    engine and renders the reply — without ever calling a real LLM provider."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.chat_engine import ChatTurnResult
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import chat as chat_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        messages.append({"role": "assistant", "content": "The answer is 42."})
        return ChatTurnResult(text="The answer is 42.", steps=[])

    monkeypatch.setattr(chat_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            # "Chat" (the nav link) renders synchronously before the async page body
            # resumes past `await client.connected()` — waiting on it is not proof the
            # settings fields exist yet. Wait on one of those instead.
            await user.should_see(marker="chat-model")
            user.find(marker="chat-model").type("openai/gpt-4o")
            user.find(marker="chat-api-key").type("sk-test")
            user.find(marker="chat-question").type("what did I spend last month?")
            user.find(marker="chat-send").click()
            await user.should_see("The answer is 42.")

    asyncio.run(_check())


def test_chat_page_renders_clarify_options_as_clickable_chips(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Clicking an option chip sends its text as the next question, rather than
    requiring the user to type a reply to the model's clarifying question."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.chat_engine import ChatTurnResult
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import chat as chat_view

    sent_questions: list[str] = []

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        sent_questions.append(question)
        if len(sent_questions) == 1:
            return ChatTurnResult(
                text="Which time window?", steps=[], options=["Last month", "Year to date"]
            )
        return ChatTurnResult(text="Here you go.", steps=[])

    monkeypatch.setattr(chat_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            await user.should_see(marker="chat-model")
            user.find(marker="chat-model").type("openai/gpt-4o")
            user.find(marker="chat-api-key").type("sk-test")
            user.find(marker="chat-question").type("what did I spend?")
            user.find(marker="chat-send").click()
            await user.should_see("Which time window?")
            user.find(marker="chat-option-1-0").click()
            await user.should_see("Here you go.")

    asyncio.run(_check())
    assert sent_questions == ["what did I spend?", "Last month"]


def test_chat_page_renders_a_tool_step_with_query_results(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Highest-risk new UI path: the tool-call transparency expansion actually
    renders when a turn's result carries a query_metric step with rows."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.chat_engine import ChatTurnResult, ToolStep
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import chat as chat_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        step = ToolStep(
            name="query_metric",
            arguments={"name": "shared.tco_summary_month"},
            rows=[
                {"charge_month": "2026-06-01", "net_cost": 1000.0},
                {"charge_month": "2026-07-01", "net_cost": 1200.0},
            ],
        )
        return ChatTurnResult(text="Spend rose from June to July.", steps=[step])

    monkeypatch.setattr(chat_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            await user.should_see(marker="chat-model")
            user.find(marker="chat-model").type("openai/gpt-4o")
            user.find(marker="chat-api-key").type("sk-test")
            user.find(marker="chat-question").type("what did I spend?")
            user.find(marker="chat-send").click()
            await user.should_see("Queried query_metric")
            await user.should_see("Spend rose from June to July.")

    asyncio.run(_check())


def test_chat_page_run_sql_step_defaults_open_others_stay_collapsed(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """run_sql is the model's own freeform SQL, not a tested view — its debug
    expansion should default open so the query is auditable at a glance,
    unlike a purpose-built tool step like query_metric."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.chat_engine import ChatTurnResult, ToolStep
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import chat as chat_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        steps = [
            ToolStep(name="list_metrics", arguments={}, rows=None),
            ToolStep(
                name="run_sql",
                arguments={"sql": "SELECT 1"},
                rows=[{"charge_month": "2026-07-01", "net_cost": 1000.0}],
            ),
        ]
        return ChatTurnResult(text="Here you go.", steps=steps)

    monkeypatch.setattr(chat_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            await user.should_see(marker="chat-model")
            user.find(marker="chat-model").type("openai/gpt-4o")
            user.find(marker="chat-api-key").type("sk-test")
            user.find(marker="chat-question").type("which service grew the most?")
            user.find(marker="chat-send").click()
            await user.should_see("Queried run_sql")

            expansions = {e.text: e.value for e in user.find(kind=ui.expansion).elements}
            assert expansions == {
                "Called list_metrics": False,
                "Queried run_sql": True,
            }

    asyncio.run(_check())


def test_chat_page_charts_despite_a_constant_dimension_column(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: aws.monthly_bill keeps `provider_name` as a column even
    once sliced/filtered to one provider (still constant, e.g. always "AWS") —
    that must not count as a second dimension and block the chart; only a
    column that actually varies (charge_month here) should."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.chat_engine import ChatTurnResult, ToolStep
    from flashlight.dashboard.router import build_pages
    from flashlight.dashboard.views import chat as chat_view

    async def fake_run_turn(messages, question, **kwargs):  # type: ignore[no-untyped-def]
        step = ToolStep(
            name="query_metric",
            arguments={"name": "aws.monthly_bill", "measures": ["net_cost"]},
            rows=[
                {"provider_name": "AWS", "charge_month": "2026-06-01", "net_cost": 1000.0},
                {"provider_name": "AWS", "charge_month": "2026-07-01", "net_cost": 1200.0},
            ],
        )
        return ChatTurnResult(text="Spend rose from June to July.", steps=[step])

    monkeypatch.setattr(chat_view, "run_turn", fake_run_turn)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            await user.should_see(marker="chat-model")
            user.find(marker="chat-model").type("openai/gpt-4o")
            user.find(marker="chat-api-key").type("sk-test")
            user.find(marker="chat-question").type("chart my spend")
            user.find(marker="chat-send").click()
            await user.should_see("Spend rose from June to July.")
            assert len(user.find(kind=ui.plotly).elements) == 1
            with pytest.raises(AssertionError):  # no table — the chart heuristic matched
                user.find(kind=ui.table)

    asyncio.run(_check())


def test_chat_settings_persist_across_a_reload_in_the_same_tab(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Provider/model/base URL persist via app.storage.general (survives a
    process restart, not just a page reload); the API key persists via the OS
    keychain (faked here with an in-memory dict — the autouse `_no_real_keyring`
    fixture blocks the real one) once the settings dialog's Done button is
    clicked, which is the only place a save is triggered."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard import chat_credentials
    from flashlight.dashboard.router import build_pages

    fake_keychain: dict[str, str] = {}
    monkeypatch.setattr(chat_credentials, "_keyring_get", fake_keychain.get)
    monkeypatch.setattr(chat_credentials, "_keyring_set", fake_keychain.__setitem__)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            await user.should_see(marker="chat-model")
            provider = next(iter(user.find(marker="chat-provider").elements))
            provider.value = "Anthropic (Claude)"  # type: ignore[attr-defined]
            user.find(marker="chat-api-key").type("sk-persisted")
            user.find(marker="chat-settings-done").click()

            await user.open("/chat")  # simulate a reload within the same tab
            await user.should_see(marker="chat-model")
            model_elem = next(iter(user.find(marker="chat-model").elements))
            api_key_elem = next(iter(user.find(marker="chat-api-key").elements))
            model_after_reload = model_elem.value  # type: ignore[attr-defined]
            api_key_after_reload = api_key_elem.value  # type: ignore[attr-defined]
            assert model_after_reload == "claude-sonnet-4-5"
            assert api_key_after_reload == "sk-persisted"
            assert fake_keychain == {"Anthropic (Claude)": "sk-persisted"}

    asyncio.run(_check())


def test_chat_falls_back_to_env_var_when_keychain_has_nothing(lake_home, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No keychain entry and no prior session — the FLASHLIGHT_CHAT_API_KEY env
    var (same *_env indirection convention as connector credentials) prefills
    the key field."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard import chat_credentials
    from flashlight.dashboard.router import build_pages

    monkeypatch.setenv(chat_credentials.ENV_VAR, "sk-from-env")

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/chat")
            await user.should_see(marker="chat-model")
            api_key_elem = next(iter(user.find(marker="chat-api-key").elements))
            assert api_key_elem.value == "sk-from-env"  # type: ignore[attr-defined]

    asyncio.run(_check())


def test_usage_page_renders_empty_state_with_no_chat_activity(lake_home) -> None:  # type: ignore[no-untyped-def]
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/usage")
            await user.should_see("Usage")
            await user.should_see("No chat activity yet")

    asyncio.run(_check())


def test_usage_page_renders_logged_chat_turns(lake_home) -> None:  # type: ignore[no-untyped-def]
    from datetime import datetime

    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages
    from flashlight.lake.chat_turns import record_chat_turn

    record_chat_turn(
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
            await user.should_see("Chat turns")
            await user.should_see("Total tokens")

    asyncio.run(_check())


def test_policy_page_shows_effective_thresholds(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A compliance verdict is only meaningful next to the threshold it was judged
    against — and the file where a user changes it."""
    from datetime import date as _date
    from decimal import Decimal as _Decimal

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
            user.find("Policy Compliance").click()
            await user.should_see("Policy thresholds")
            # The default ceiling, shown so the verdict above is interpretable.
            await user.should_see("60")

    asyncio.run(_check())
