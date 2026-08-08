"""Boot must survive a read-only lake home — the prerequisite for `docker run --read-only`.

Two independent writes used to happen unconditionally at startup, and either one failing
took the whole dashboard down before it served a byte:

* ``lake/duck.py::connect`` mkdir'd the DuckDB spill dir. This is on *every* query path
  (dashboard, MCP, ingest, transform), so an unwritable lake home meant every query failed.
* ``dashboard/launch.py`` mkdir'd NiceGUI's storage dir.

Both are now best-effort, and ``FLASHLIGHT_DUCKDB_TEMP_DIR`` gives a deployment somewhere
writable to point at. The soft-fail is the more important half: it holds even when the
deployment forgets to set the override.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from flashlight.core.settings import get_settings


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[Path]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_spill_dir_defaults_under_the_lake_home(lake_home: Path) -> None:
    """The default must not change: a big transform should spill onto the lake's volume."""
    from flashlight.lake import paths

    assert paths.duckdb_temp_dir() == lake_home / "tmp" / "duckdb"


def test_spill_dir_honors_the_env_override(lake_home: Path, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import duck, paths

    override = tmp_path / "elsewhere" / "duckdb"
    monkeypatch.setenv("FLASHLIGHT_DUCKDB_TEMP_DIR", str(override))
    get_settings.cache_clear()

    assert paths.duckdb_temp_dir() == override
    con = duck.connect()
    try:
        # Asserted through DuckDB itself, not just the path helper — the point is that the
        # engine actually spills there.
        configured = con.execute("SELECT current_setting('temp_directory')").fetchone()
        assert configured is not None
        assert str(override) in str(configured[0])
    finally:
        con.close()
    assert override.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode bits"
)
def test_connect_survives_an_unwritable_spill_dir(lake_home: Path, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A read-only lake home must degrade to "big queries fail", not "nothing works"."""
    from flashlight.lake import duck

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)  # r-x: cannot create children
    monkeypatch.setenv("FLASHLIGHT_DUCKDB_TEMP_DIR", str(locked / "duckdb"))
    get_settings.cache_clear()
    # The module warns once per process, and another test may have tripped it already.
    monkeypatch.setattr(duck, "_temp_dir_warned", False)

    try:
        con = duck.connect()  # must not raise
        try:
            assert con.execute("SELECT 1").fetchone() == (1,)
        finally:
            con.close()
    finally:
        locked.chmod(0o700)


def test_storage_path_respects_a_preset_value(lake_home: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A read-only deployment sets NICEGUI_STORAGE_PATH itself; boot must not override it,
    and must not touch the lake home at all."""
    from flashlight.dashboard.launch import prepare_storage_path

    preset = str(lake_home / "preset-elsewhere")
    monkeypatch.setenv("NICEGUI_STORAGE_PATH", preset)

    prepare_storage_path()

    assert os.environ["NICEGUI_STORAGE_PATH"] == preset
    assert not (lake_home / "meta" / "dashboard_storage").exists(), (
        "the lake-home default must not be created when the caller supplied a path"
    )


def test_storage_path_defaults_into_the_lake(lake_home: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard.launch import prepare_storage_path

    monkeypatch.delenv("NICEGUI_STORAGE_PATH", raising=False)

    prepare_storage_path()

    expected = lake_home / "meta" / "dashboard_storage"
    assert expected.is_dir()
    assert os.environ["NICEGUI_STORAGE_PATH"] == str(expected)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode bits"
)
def test_storage_path_degrades_on_an_unwritable_lake_home(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The regression this guards: an unwritable lake home used to raise here, killing the
    dashboard at boot before it served a single page."""
    from flashlight.dashboard.launch import prepare_storage_path

    locked = tmp_path / "locked-home"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("FLASHLIGHT_HOME", str(locked))
    monkeypatch.delenv("NICEGUI_STORAGE_PATH", raising=False)
    get_settings.cache_clear()

    try:
        prepare_storage_path()  # must not raise
    finally:
        locked.chmod(0o700)
        get_settings.cache_clear()

    # Left unset rather than pointed somewhere broken, so NiceGUI falls back to its own
    # default instead of failing on a path it cannot create.
    assert "NICEGUI_STORAGE_PATH" not in os.environ
