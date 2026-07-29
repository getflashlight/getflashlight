"""Usage — your own BYOK chat activity: turns, tokens, tool calls.

Reads the ``chat_turns`` log (:mod:`flashlight.lake.chat_turns`) — token
counts and timestamps only, never message text or API keys (see that
module's docstring for the privacy stance).
"""

from __future__ import annotations

import pandas as pd

from flashlight.dashboard import chrome
from flashlight.dashboard.data import telemetry_df


def render() -> None:
    chrome.section_title("Usage")
    chrome.section_caption("Your own BYOK chat activity — nothing here is shared externally.")

    df = telemetry_df(
        "SELECT turn_id, session_id, model, prompt_tokens, completion_tokens, "
        "total_tokens, tool_call_count, occurred_at "
        "FROM telemetry.chat_turn ORDER BY occurred_at DESC"
    )
    if df.empty:
        chrome.section_caption("No chat activity yet — ask something on the Chat page.")
        return

    chrome.kpi_row(
        [
            ("Chat turns", f"{len(df):,}", "total"),
            (
                "Total tokens",
                f"{int(df['total_tokens'].fillna(0).sum()):,}",
                "prompt + completion",
            ),
            ("Sessions", f"{df['session_id'].nunique():,}", "distinct browser tabs"),
            ("Tool calls", f"{int(df['tool_call_count'].fillna(0).sum()):,}", "across all turns"),
        ]
    )

    display = df.drop(columns=["turn_id"]).copy()
    display["occurred_at"] = pd.to_datetime(display["occurred_at"]).dt.strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    with chrome.panel():
        chrome.panel_title("Recent chat turns")
        chrome.searchable_table(
            display,
            key="chat_turns",
            search_col="model",
            int_cols=["prompt_tokens", "completion_tokens", "total_tokens", "tool_call_count"],
            rename={
                "session_id": "Session",
                "model": "Model",
                "prompt_tokens": "Prompt tokens",
                "completion_tokens": "Completion tokens",
                "total_tokens": "Total tokens",
                "tool_call_count": "Tool calls",
                "occurred_at": "When",
            },
        )
