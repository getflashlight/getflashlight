"""Tests for ``dashboard/chrome.py``'s shared NiceGUI helpers."""

from __future__ import annotations

import asyncio


def test_lazy_tab_panels_builds_only_the_active_tab_until_clicked() -> None:
    """The first tab builds on load; every other tab's content waits for its
    first click, and is built at most once (switching back is a free show/hide,
    not a re-render) — the fix for /databricks paying for ~9 tabs to show 1.
    """
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard import chrome

    built: list[str] = []

    def _panel(name: str):  # type: ignore[no-untyped-def]
        def _render() -> None:
            built.append(name)
            ui.label(f"content-{name}")

        return _render

    async def _check() -> None:
        async with user_simulation() as user:

            @ui.page("/lazy-tabs-test")
            def _page() -> None:
                chrome.lazy_tab_panels(
                    [("A", _panel("A")), ("B", _panel("B")), ("C", _panel("C"))]
                )

            await user.open("/lazy-tabs-test")
            # Only the first tab is built on load.
            await user.should_see("content-A")
            await user.should_not_see("content-B")
            await user.should_not_see("content-C")
            assert built == ["A"]

            # Clicking a different tab builds exactly that tab, once.
            user.find(kind=ui.tab, content="B").click()
            await user.should_see("content-B")
            assert built == ["A", "B"]

            # Switching back to an already-built tab is a show/hide, not a rebuild.
            user.find(kind=ui.tab, content="A").click()
            await user.should_see("content-A")
            user.find(kind=ui.tab, content="B").click()
            await user.should_see("content-B")
            assert built == ["A", "B"], "revisiting a tab must not build it again"

            # The still-unvisited third tab stays unbuilt throughout.
            assert "C" not in built

    asyncio.run(_check())
