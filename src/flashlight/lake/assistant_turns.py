"""BYOK assistant usage log — ``runlog.py``'s sibling for the ``/assistant`` page.

Each assistant turn appends one Parquet file under ``meta/assistant_turns/`` (one file per
turn, same append-only/no-read-modify-write-races rationale as ``runlog.py``).
Deliberately narrow: no message text and no API key are ever recorded — only
enough for the user to see their own usage (model, token counts, timestamp) on
the ``/usage`` page.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa

from flashlight.core.logging import get_logger
from flashlight.lake import paths

logger = get_logger(__name__)

_TS = pa.timestamp("us", tz="UTC")

# Every column is nullable, and the latency ones were added after the token ones
# — turns recorded by an older Flashlight simply have NULL there. See
# flashlight.lake.duck.register_assistant_turns for the two things that keep an
# older file readable once this schema grows again.
ASSISTANT_TURN_SCHEMA: pa.Schema = pa.schema(
    [
        ("turn_id", pa.string()),
        ("session_id", pa.string()),
        ("model", pa.string()),
        ("prompt_tokens", pa.int64()),
        ("completion_tokens", pa.int64()),
        ("total_tokens", pa.int64()),
        ("tool_call_count", pa.int64()),
        ("occurred_at", _TS),
        # --- Per-turn latency, so "why is the assistant slow?" is answerable
        # from data instead of from token counts times a guessed throughput.
        # duration_ms is the whole turn; the four node columns are what it split
        # into (a node can run more than once, so they accumulate).
        ("duration_ms", pa.float64()),
        ("plan_ms", pa.float64()),
        ("explore_ms", pa.float64()),
        ("execute_ms", pa.float64()),
        ("synthesize_ms", pa.float64()),
        # How much work the turn took to get there: LLM requests (pydantic-ai's
        # own count, so output-validation retries are included), how many times
        # it re-planned, and how many rounds _QuirkTransport silently re-issued
        # because the provider returned nothing usable.
        ("llm_request_count", pa.int64()),
        ("plan_pass_count", pa.int64()),
        ("empty_round_retries", pa.int64()),
        # answer | clarify | no_answer | error — so the share of turns that
        # ended with nothing useful is a tracked number, not an anecdote.
        ("outcome", pa.string()),
        # summary_spec | caption | model — where the answer's wording came from.
        # summary_spec and caption both skip the synthesis call (the model declared
        # the sentence at plan time, or a fixed assembly was used); model means a
        # real second round trip. Makes the saving measurable rather than asserted.
        ("answer_source", pa.string()),
    ]
)


def empty_table() -> pa.Table:
    """An empty, fully-typed assistant-turn table — the no-data fallback for readers."""
    return ASSISTANT_TURN_SCHEMA.empty_table()


def record_assistant_turn(
    *,
    turn_id: str,
    session_id: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    tool_call_count: int,
    occurred_at: datetime,
    timings: dict[str, float | int | str | None] | None = None,
) -> None:
    """Append one assistant turn to the log (best-effort; never raises).

    *timings* carries the latency/outcome columns (see
    :data:`ASSISTANT_TURN_SCHEMA`); unknown keys are ignored and absent ones land
    as NULL, so a caller that doesn't measure — or a future column this one
    doesn't know about — still writes a valid row.
    """
    try:
        import pyarrow.parquet as pq

        paths.assistant_turns_dir().mkdir(parents=True, exist_ok=True)
        row: dict[str, object] = {
            "turn_id": turn_id,
            "session_id": session_id,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "tool_call_count": tool_call_count,
            "occurred_at": occurred_at,
        }
        for name in ASSISTANT_TURN_SCHEMA.names:
            row.setdefault(name, (timings or {}).get(name))
        table = pa.Table.from_pylist([row], schema=ASSISTANT_TURN_SCHEMA)
        pq.write_table(table, paths.assistant_turns_dir() / f"{turn_id}.parquet")
    except Exception as exc:  # noqa: BLE001 - observability must not break assistant
        logger.warning("assistant_turn_write_failed", turn_id=turn_id, error=str(exc))
