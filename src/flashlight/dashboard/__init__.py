"""NiceGUI dashboard — the human consumer surface (replaces Grafana).

A pure-Python app that reads the published GOLD Parquet read-only via its own
in-memory DuckDB, so it ships in the wheel and needs no server or datasource
plugin. Launched by ``flashlight dashboard serve`` (see :mod:`.launch`).
"""

from __future__ import annotations
