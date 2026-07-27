"""The GOLD read surface — the one query module both consumers share.

The MCP server and the NiceGUI dashboard import :mod:`flashlight.gold.reader`, so
the metrics-access logic (catalogued views, the ad-hoc SELECT guard rails) lives
once and two thin frontends consume it. Reads go through a throwaway in-memory
DuckDB over the published ``gold/*.parquet`` — never raw/silver.
"""

from __future__ import annotations
