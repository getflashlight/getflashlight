"""Snowflake minimum supported driver versions.

Reference: https://docs.snowflake.com/en/release-notes/requirements
Last updated: 2026-02-01 (matches Snowflake's published minimum versions as of that date).

The CLIENT_APPLICATION_ID column in ACCOUNT_USAGE.SESSIONS carries a combined
"DriverName Version" string (e.g. "PythonConnector 4.7.1", "Go 2.0.2"). This module
parses that format and checks against the documented minimums.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class DriverVersion(NamedTuple):
    major: int
    minor: int
    patch: int


# Maps the prefix seen in CLIENT_APPLICATION_ID to (display_name, minimum_version).
# Keys are lowercased for case-insensitive matching.
_SUPPORTED_DRIVERS: dict[str, tuple[str, DriverVersion]] = {
    "pythonconnector": ("Snowflake Connector for Python", DriverVersion(3, 7, 0)),
    "snowflakesqlalchemy": ("Snowflake SQLAlchemy", DriverVersion(1, 5, 1)),
    "go": ("Go Snowflake Driver", DriverVersion(1, 7, 2)),
    "javascript": ("Node.js Driver", DriverVersion(1, 9, 3)),
    "jdbc": ("JDBC Driver", DriverVersion(3, 14, 5)),
    "odbc": ("ODBC Driver", DriverVersion(3, 2, 0)),
    "dotnetdriver": (".NET Driver", DriverVersion(2, 2, 0)),
    ".netdriver": (".NET Driver", DriverVersion(2, 2, 0)),
    "snowsql": ("SnowSQL", DriverVersion(1, 4, 0)),
    "snowflakecli": ("Snowflake CLI", DriverVersion(2, 4, 0)),
    "phppdodriver": ("PHP PDO Driver", DriverVersion(2, 0, 1)),
    "snowpark": ("Snowpark Library for Python", DriverVersion(1, 0, 0)),
    "kafkaconnector": ("Snowflake Connector for Kafka", DriverVersion(2, 1, 2)),
    "sparkconnector": ("Snowflake Connector for Spark", DriverVersion(2, 14, 0)),
}

_VERSION_RE = re.compile(r"^(.+?)\s+(\d+)\.(\d+)\.(\d+)$")


def _parse_client_application_id(value: str) -> tuple[str, DriverVersion | None]:
    """Parse 'DriverName X.Y.Z' into (lowercase_driver_key, version) or (raw, None)."""
    m = _VERSION_RE.match(value.strip())
    if not m:
        return (value.strip().lower(), None)
    name = m.group(1).replace(" ", "").lower()
    return (name, DriverVersion(int(m.group(2)), int(m.group(3)), int(m.group(4))))


def check_support_status(client_application_id: str | None) -> str:
    """Return 'supported', 'unsupported', or 'unknown' for a CLIENT_APPLICATION_ID value.

    'unknown' means the driver is not in our reference table (e.g. Snowsight, internal
    tools, or third-party integrations we don't track).
    """
    if not client_application_id:
        return "unknown"

    key, version = _parse_client_application_id(client_application_id)

    if key not in _SUPPORTED_DRIVERS:
        return "unknown"

    if version is None:
        return "unknown"

    _, min_version = _SUPPORTED_DRIVERS[key]
    if version >= min_version:
        return "supported"
    return "unsupported"
