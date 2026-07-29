from __future__ import annotations

from datetime import UTC, datetime

from flashlight.core.settings import get_settings
from flashlight.lake import chat_turns, duck


def test_record_chat_turn_round_trips(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    chat_turns.record_chat_turn(
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
        duck.register_chat_turns(con)
        rows = con.execute("SELECT * FROM telemetry.chat_turn").fetchdf().to_dict("records")
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


def test_register_chat_turns_empty_fallback_is_typed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    con = duck.connect()
    try:
        duck.register_chat_turns(con)
        df = con.execute("SELECT * FROM telemetry.chat_turn").fetchdf()
    finally:
        con.close()

    assert df.empty
    assert list(df.columns) == list(chat_turns.CHAT_TURN_SCHEMA.names)

    get_settings.cache_clear()


def test_record_chat_turn_never_raises_on_write_failure(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A telemetry write failure (e.g. an unwritable disk) must not break chat."""
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path / "does" / "not" / "exist"))
    monkeypatch.setattr(
        "flashlight.lake.paths.chat_turns_dir",
        lambda: (_ for _ in ()).throw(OSError("nope")),
    )
    get_settings.cache_clear()

    chat_turns.record_chat_turn(
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
