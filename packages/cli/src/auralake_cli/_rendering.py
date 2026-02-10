"""Rich-based rendering helpers for the CLI.

Re-exports from core.output for backward compatibility.
After Phase 3 (HTTP client refactor), this module will be the sole
location for all Rich rendering — the CLI's presentation layer.
"""

from __future__ import annotations

from auralake_shared.core.output import (
    OutputFormat,
    confirm_action,
    console,
    error_console,
    print_error,
    print_success,
    print_warning,
    render_recommendations,
    render_table,
)

__all__ = [
    "OutputFormat",
    "confirm_action",
    "console",
    "error_console",
    "print_error",
    "print_success",
    "print_warning",
    "render_recommendations",
    "render_table",
]
