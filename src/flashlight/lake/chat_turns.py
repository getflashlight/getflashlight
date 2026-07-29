"""BYOK chat usage log — the sibling of ``runlog.py`` for the dashboard's ``/chat``
page.

Each chat turn appends one Parquet file under ``meta/chat_turns/`` (one file per
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
CHAT_TURN_SCHEMA: pa.Schema = pa.schema(
    [
        ("turn_id", pa.string()),
        ("session_id", pa.string()),
        ("model", pa.string()),
        ("prompt_tokens", pa.int64()),
        ("completion_tokens", pa.int64()),
        ("total_tokens", pa.int64()),
        ("tool_call_count", pa.int64()),
        ("occurred_at", _TS),
    ]
)


def empty_table() -> pa.Table:
    """An empty, fully-typed chat-turn table — the no-data fallback for readers."""
    return CHAT_TURN_SCHEMA.empty_table()


def record_chat_turn(
    *,
    turn_id: str,
    session_id: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    tool_call_count: int,
    occurred_at: datetime,
) -> None:
    """Append one chat turn to the log (best-effort; never raises)."""
    try:
        import pyarrow.parquet as pq

        paths.chat_turns_dir().mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [
                {
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "tool_call_count": tool_call_count,
                    "occurred_at": occurred_at,
                }
            ],
            schema=CHAT_TURN_SCHEMA,
        )
        pq.write_table(table, paths.chat_turns_dir() / f"{turn_id}.parquet")
    except Exception as exc:  # noqa: BLE001 - observability must not break chat
        logger.warning("chat_turn_write_failed", turn_id=turn_id, error=str(exc))
