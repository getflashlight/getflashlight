from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from flashlight.core.settings import get_settings
from flashlight.lake import assistant_turns, duck, paths


def test_record_assistant_turn_round_trips(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    assistant_turns.record_assistant_turn(
        turn_id="t1",
        session_id="s1",
        model="openai/gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        tool_call_count=2,
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )

    con = duck.connect()
    try:
        duck.register_assistant_turns(con)
        rows = con.execute("SELECT * FROM telemetry.assistant_turn").fetchdf().to_dict("records")
    finally:
        con.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["turn_id"] == "t1"
    assert row["session_id"] == "s1"
    assert row["model"] == "openai/gpt-4o"
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 5
    assert row["total_tokens"] == 15
    assert row["tool_call_count"] == 2

    get_settings.cache_clear()


def test_register_assistant_turns_empty_fallback_is_typed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    con = duck.connect()
    try:
        duck.register_assistant_turns(con)
        df = con.execute("SELECT * FROM telemetry.assistant_turn").fetchdf()
    finally:
        con.close()

    assert df.empty
    assert list(df.columns) == list(assistant_turns.ASSISTANT_TURN_SCHEMA.names)

    get_settings.cache_clear()


def _write_turn(path, schema: pa.Schema, **values: object) -> None:  # type: ignore[no-untyped-def]
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {name: values.get(name) for name in schema.names}
    pq.write_table(pa.Table.from_pylist([row], schema=schema), path)


def test_register_assistant_turns_reads_old_and_new_schemas_together(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A turn written before a schema column existed must still read back.

    The log is append-only and never rewritten, so the day a new column lands
    every older file on disk disagrees with it. Without ``union_by_name`` DuckDB
    rejects the whole glob, which would take the ``/usage`` page down for
    everything already recorded — the regression this pins.
    """
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    schema = assistant_turns.ASSISTANT_TURN_SCHEMA
    dropped = "tool_call_count"
    old_schema = schema.remove(schema.get_field_index(dropped))
    _write_turn(
        paths.assistant_turns_dir() / "old.parquet",
        old_schema,
        turn_id="old",
        session_id="s1",
        model="m",
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )
    _write_turn(
        paths.assistant_turns_dir() / "new.parquet",
        schema,
        turn_id="new",
        session_id="s1",
        model="m",
        tool_call_count=3,
        occurred_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
    )

    con = duck.connect()
    try:
        duck.register_assistant_turns(con)
        df = con.execute(
            "SELECT turn_id, tool_call_count FROM telemetry.assistant_turn ORDER BY turn_id"
        ).fetchdf()
    finally:
        con.close()

    assert list(df["turn_id"]) == ["new", "old"]
    by_turn = dict(zip(df["turn_id"], df[dropped], strict=True))
    assert by_turn["new"] == 3
    assert pd.isna(by_turn["old"])

    get_settings.cache_clear()


def test_register_assistant_turns_view_schema_survives_an_all_old_files_lake(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch
) -> None:
    """Every schema column must be selectable even if no file on disk has it.

    ``views/usage.py`` names its columns explicitly, so a lake holding only
    pre-upgrade turns would otherwise fail to bind the new ones.
    """
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    schema = assistant_turns.ASSISTANT_TURN_SCHEMA
    _write_turn(
        paths.assistant_turns_dir() / "old.parquet",
        pa.schema([schema.field("turn_id"), schema.field("occurred_at")]),
        turn_id="old",
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
    )

    con = duck.connect()
    try:
        duck.register_assistant_turns(con)
        df = con.execute("SELECT * FROM telemetry.assistant_turn").fetchdf()
    finally:
        con.close()

    assert list(df.columns) == list(schema.names)
    assert len(df) == 1

    get_settings.cache_clear()


def test_register_assistant_turns_still_reads_the_pre_rename_dir(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """History written as ``meta/chat_turns/`` must not disappear from /usage.

    The chat -> assistant rename moved the directory; a real install had 97 turns
    under the old name and an empty log under the new one, so the page showed
    nothing at all.
    """
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    schema = assistant_turns.ASSISTANT_TURN_SCHEMA
    _write_turn(
        paths.legacy_assistant_turns_dir() / "legacy.parquet",
        schema,
        turn_id="legacy",
        session_id="s1",
        model="m",
        total_tokens=42,
        tool_call_count=0,
        occurred_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
    )

    con = duck.connect()
    try:
        duck.register_assistant_turns(con)
        rows = con.execute(
            "SELECT turn_id, total_tokens FROM telemetry.assistant_turn"
        ).fetchall()
    finally:
        con.close()

    assert rows == [("legacy", 42)]

    get_settings.cache_clear()


def test_record_assistant_turn_never_raises_on_write_failure(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A telemetry write failure (e.g. an unwritable disk) must not break assistant."""
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path / "does" / "not" / "exist"))
    monkeypatch.setattr(
        "flashlight.lake.paths.assistant_turns_dir",
        lambda: (_ for _ in ()).throw(OSError("nope")),
    )
    get_settings.cache_clear()

    assistant_turns.record_assistant_turn(
        turn_id="t1",
        session_id="s1",
        model="openai/gpt-4o",
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        tool_call_count=0,
        occurred_at=datetime.now(UTC),
    )  # must not raise

    get_settings.cache_clear()
