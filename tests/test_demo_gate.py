"""What ``FLASHLIGHT_DEMO=1`` actually gates — previously untested entirely.

The demo image is meant to be a strictly read-only site: no ingestion, no BYOK assistant, no
MCP. The write/egress surfaces are gated off (see ``_GATED_ROUTES``), and the negative case
matters as much as the positive one — a gate that was accidentally always-on would look
identical here to a gate that works, so every test has a non-demo counterpart.

``get_settings`` is ``lru_cache``d and ``build_pages()`` reads the flag at *registration*
time, so the env var has to be set (and the cache cleared) before build_pages runs — see
the ordering in each test.

Not covered here, because it isn't in-app behaviour: anyone with ``docker exec`` on the
container can still run the full CLI (``cleanup --yes``, ``ingest``, ``aws delete-export``).
demo/README.md says so explicitly rather than implying the flag is a sandbox.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

import pytest

from flashlight.core.settings import get_settings

_GATED_ROUTES = ("/connections", "/assistant", "/usage", "/mcp-server")


@pytest.fixture
def demo_lake(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    monkeypatch.setenv("FLASHLIGHT_DEMO", "1")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def normal_lake(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    monkeypatch.delenv("FLASHLIGHT_DEMO", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _registered_paths() -> set[str]:
    """The literal page routes ``build_pages()`` registers, read off NiceGUI's registry.

    Must run inside ``user_simulation()``: NiceGUI's route registry is process-global and
    ``build_pages`` appends to it, so reading it bare picks up every route any earlier test
    in the session registered — which made this assertion pass alone and fail in the suite.
    The simulation context resets the registry first (same reason test_dashboard.py calls
    build_pages inside it).
    """
    import asyncio

    from nicegui import app
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    paths: set[str] = set()

    async def _collect() -> None:
        async with user_simulation():
            build_pages()
            paths.update(
                str(path) for r in app.routes if (path := getattr(r, "path", None)) is not None
            )

    asyncio.run(_collect())
    return paths


def test_demo_mode_does_not_register_the_write_surfaces(demo_lake) -> None:  # type: ignore[no-untyped-def]
    """Routes are absent, not merely hidden — so they 404 rather than being URL-reachable."""
    paths = _registered_paths()

    for route in _GATED_ROUTES:
        assert route not in paths, f"{route} must not be registered in demo mode"
    # The read-only surface must still be there — a gate that took it out would be
    # "working" by this file's other assertions while shipping a blank demo. Home plus the
    # "/{group}" catch-all *is* that surface now: utilization and the leaderboards became
    # tabs on each provider page, so they no longer have routes of their own.
    assert "/" in paths
    assert "/{group}" in paths
    # Retired URLs redirect in demo mode too — those aren't gated, they're 307s to Home.
    assert "/utilization" in paths
    assert "/leaderboard" in paths


def test_normal_mode_registers_the_write_surfaces(normal_lake) -> None:  # type: ignore[no-untyped-def]
    """The negative case: without this, an always-on gate would pass every test above."""
    paths = _registered_paths()

    for route in _GATED_ROUTES:
        assert route in paths, f"{route} must be registered when not in demo mode"


def test_demo_mode_hides_the_write_surfaces_from_nav(demo_lake) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard.router import _fixed_nav

    hrefs = {href for href, _, _ in _fixed_nav()}  # noqa: SLF001 - our own module

    assert "/connections" not in hrefs
    assert "/assistant" not in hrefs
    # /mcp-server starts a subprocess that opens an unauthenticated port serving ad-hoc SQL
    # — the one page whose whole purpose is the thing serve_mcp refuses to do in demo mode.
    assert "/mcp-server" not in hrefs
    # /usage was never in nav (its only link is a button on the assistant page), which is how
    # it stayed registered-but-invisible for so long.
    # Home is the only fixed row a demo visitor sees; everything else in nav is a provider
    # row, built separately from the group list (_nav_groups). The subset assertion is
    # stronger than "/" in hrefs: it also catches a write surface leaking back in.
    assert hrefs <= {"/", "/docs"}
    assert "/" in hrefs


def test_normal_mode_shows_the_write_surfaces_in_nav(normal_lake) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard.router import _fixed_nav

    hrefs = {href for href, _, _ in _fixed_nav()}  # noqa: SLF001 - our own module

    assert "/connections" in hrefs
    assert "/assistant" in hrefs
    assert "/mcp-server" in hrefs


def test_serve_mcp_refuses_in_demo_mode(demo_lake) -> None:  # type: ignore[no-untyped-def]
    """The MCP server has no auth and exposes run_sql; it must not start on a demo lake."""
    from flashlight.mcp.server import serve_mcp

    with pytest.raises(SystemExit) as exc:
        serve_mcp()

    message = str(exc.value)
    assert "FLASHLIGHT_DEMO" in message
    assert "run_sql" in message


# Run in a subprocess: by the time this module executes, other tests in the session have
# already imported flashlight.mcp.server, so an in-process `sys.modules` check would
# always fail regardless of the router's behaviour.
_IMPORT_PROBE = """
import sys
from flashlight.dashboard.router import build_pages
build_pages()
print("MCP_IMPORTED" if "flashlight.mcp.server" in sys.modules else "MCP_ABSENT")
"""


def _probe(env_extra: dict[str, str], tmp_path: object) -> str:
    import os

    env = {k: v for k, v in os.environ.items() if k != "FLASHLIGHT_DEMO"}
    env["FLASHLIGHT_HOME"] = str(tmp_path)
    env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


def test_demo_mode_never_loads_the_mcp_or_llm_stack(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Lazy-importing the assistant view keeps keyring, pydantic-ai and the MCP tool registry
    out of the demo process entirely — so there is no in-process path to ``run_sql`` even
    if a future route slipped through the gate."""
    assert _probe({"FLASHLIGHT_DEMO": "1"}, tmp_path) == "MCP_ABSENT"


def test_normal_mode_does_load_the_mcp_stack(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The counterpart: proves the probe above is actually detecting the import, rather
    than the module simply never being imported by build_pages under any setting."""
    assert _probe({}, tmp_path) == "MCP_IMPORTED"
