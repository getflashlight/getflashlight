"""Usage — your own BYOK assistant activity: turns, tokens, tool calls.

Reads the ``assistant_turns`` log (:mod:`flashlight.lake.assistant_turns`) — token
counts and timestamps only, never message text or API keys (see that
module's docstring for the privacy stance).
"""

from __future__ import annotations

import pandas as pd

from flashlight.dashboard import chrome
from flashlight.dashboard.data import telemetry_df


def render() -> None:
    chrome.section_title("Usage")
    chrome.section_caption("Your own BYOK assistant activity — nothing here is shared externally.")

    df = telemetry_df(
        "SELECT turn_id, session_id, model, prompt_tokens, completion_tokens, "
        "total_tokens, tool_call_count, duration_ms, plan_ms, explore_ms, execute_ms, "
        "synthesize_ms, llm_request_count, plan_pass_count, empty_round_retries, outcome, "
        "answer_source, route, intent, time_to_first_result_ms, output_retries, occurred_at "
        "FROM telemetry.assistant_turn ORDER BY occurred_at DESC"
    )
    if df.empty:
        chrome.section_caption("No assistant activity yet — ask something on the Assistant page.")
        return

    # Turns recorded before the latency columns existed have NULL in all of them
    # (see lake.duck.register_assistant_turns), so every figure below is over the
    # measured subset and says so rather than counting a NULL as a fast turn.
    timed = df[df["duration_ms"].notna()]
    if timed.empty:
        latency_kpi = ("Median turn", "—", "no timed turns recorded yet")
    else:
        answered = (timed["outcome"] == "answer").sum()
        latency_kpi = (
            "Median turn",
            f"{timed['duration_ms'].median() / 1000:,.1f}s",
            f"p90 {timed['duration_ms'].quantile(0.9) / 1000:,.1f}s "
            f"· {answered / len(timed):.0%} answered ({len(timed):,} timed)",
        )

    # Where answers came from. "summary_spec" and "caption" both skipped the second
    # model call; "model" paid for it. This is the measurement that says whether the
    # deterministic path is actually firing against a real model, rather than
    # silently degrading — so it belongs next to the latency it explains.
    answered = df[df["answer_source"].notna()]
    if answered.empty:
        source_kpi = ("Answered without an LLM", "—", "no turns recorded since this shipped")
    else:
        skipped = answered["answer_source"].isin(["summary_spec", "caption"]).sum()
        by_source = answered["answer_source"].value_counts()
        detail = " · ".join(f"{source} {count}" for source, count in by_source.items())
        source_kpi = (
            "Answered without an LLM",
            f"{skipped / len(answered):.0%}",
            f"{detail} (of {len(answered):,})",
        )

    chrome.kpi_row(
        [
            ("Assistant turns", f"{len(df):,}", "total"),
            latency_kpi,
            source_kpi,
            (
                "Total tokens",
                f"{int(df['total_tokens'].fillna(0).sum()):,}",
                "prompt + completion",
            ),
            ("Tool calls", f"{int(df['tool_call_count'].fillna(0).sum()):,}", "across all turns"),
        ]
    )

    routed = df[df["route"].notna()]
    if not routed.empty:
        fast = routed[routed["route"] == "deterministic"]
        fast_detail = "no deterministic turns yet"
        if not fast.empty and fast["duration_ms"].notna().any():
            timed_fast = fast[fast["duration_ms"].notna()]["duration_ms"]
            fast_detail = (
                f"p50 {timed_fast.median() / 1000:,.1f}s · "
                f"p90 {timed_fast.quantile(0.9) / 1000:,.1f}s"
            )
        chrome.section_caption(
            f"Fast path: {len(fast) / len(routed):.0%} of routed turns · {fast_detail}."
        )

    if not timed.empty:
        with chrome.panel():
            chrome.panel_title("Where a turn's time goes")
            phases = pd.DataFrame(
                {
                    "Phase": ["Plan", "Explore", "Execute (DuckDB)", "Synthesize"],
                    "Median": [
                        timed[col].median()
                        for col in ("plan_ms", "explore_ms", "execute_ms", "synthesize_ms")
                    ],
                    "Total": [
                        timed[col].fillna(0).sum()
                        for col in ("plan_ms", "explore_ms", "execute_ms", "synthesize_ms")
                    ],
                }
            )
            phases["Share of turn"] = phases["Total"].map(
                lambda total: f"{total / max(timed['duration_ms'].fillna(0).sum(), 1):.0%}"
            )
            phases["Median"] = phases["Median"].map(lambda ms: f"{ms:,.0f} ms")
            phases["Total"] = phases["Total"].map(lambda ms: f"{ms / 1000:,.1f}s")
            chrome.flat_table(phases, key="assistant_turn_phases")
            chrome.section_caption(
                "Plan and Synthesize are model calls; Explore and Execute are local DuckDB "
                "reads. A turn that re-planned counts each pass."
            )

    display = df.drop(columns=["turn_id", "plan_ms", "explore_ms", "execute_ms"]).copy()
    display["duration_ms"] = display["duration_ms"].map(
        lambda ms: "—" if pd.isna(ms) else f"{ms / 1000:,.1f}s"
    )
    display["synthesize_ms"] = display["synthesize_ms"].map(
        lambda ms: "—" if pd.isna(ms) else f"{ms / 1000:,.1f}s"
    )
    display["time_to_first_result_ms"] = display["time_to_first_result_ms"].map(
        lambda ms: "—" if pd.isna(ms) else f"{ms / 1000:,.1f}s"
    )
    display["occurred_at"] = pd.to_datetime(display["occurred_at"]).dt.strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    with chrome.panel():
        chrome.panel_title("Recent assistant turns")
        chrome.searchable_table(
            display,
            key="assistant_turns",
            search_col="model",
            int_cols=[
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "tool_call_count",
                "llm_request_count",
                "plan_pass_count",
                "empty_round_retries",
                "output_retries",
            ],
            rename={
                "session_id": "Session",
                "model": "Model",
                "prompt_tokens": "Prompt tokens",
                "completion_tokens": "Completion tokens",
                "total_tokens": "Total tokens",
                "tool_call_count": "Tool calls",
                "duration_ms": "Turn",
                "synthesize_ms": "Synthesize",
                "llm_request_count": "LLM calls",
                "plan_pass_count": "Plan passes",
                "empty_round_retries": "Empty rounds",
                "outcome": "Outcome",
                "answer_source": "Answered by",
                "route": "Route",
                "intent": "Intent",
                "time_to_first_result_ms": "First result",
                "output_retries": "Output retries",
                "occurred_at": "When",
            },
        )
