"""Confirms the committed ``demo/lake/`` dataset (shipped in the self-host Docker
image) still fires every waste category and policy status it's meant to
demonstrate — catches drift (a hand-edited Parquet file, a `waste_rules.py`/
`policy_rules.py` change) without needing to regenerate anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flashlight.core.settings import get_settings
from scripts.build_demo_lake import verify_demo_lake

_DEMO_LAKE = Path(__file__).parents[1] / "demo" / "lake"


@pytest.mark.skipif(not _DEMO_LAKE.exists(), reason="demo/lake/ not present in this checkout")
def test_committed_demo_lake_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLASHLIGHT_HOME", str(_DEMO_LAKE))
    get_settings.cache_clear()
    try:
        verify_demo_lake()
    finally:
        get_settings.cache_clear()

