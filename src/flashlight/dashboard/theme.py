"""Framework-agnostic formatting/color helpers shared across dashboard views.

Pure Python only — no rendering. Visual chrome (panels, KPI cards, Plotly styling,
tables) lives in :mod:`flashlight.dashboard.chrome`, the NiceGUI-specific layer.
"""

from __future__ import annotations

from collections.abc import Iterable

# Stable provider identity — same hue everywhere (charts, KPIs, nav dots).
GROUP_COLORS: dict[str, str] = {
    "aws": "#FF9900",
    "databricks": "#0E7C86",
    "google": "#4285F4",
    "microsoft": "#0078D4",
}
PROVIDER_COLORS: dict[str, str] = {
    "AWS": GROUP_COLORS["aws"],
    "Amazon Web Services": GROUP_COLORS["aws"],
    "Databricks": GROUP_COLORS["databricks"],
    "Google Cloud": GROUP_COLORS["google"],
    "Google": GROUP_COLORS["google"],
    "Microsoft": GROUP_COLORS["microsoft"],
    "Microsoft Azure": GROUP_COLORS["microsoft"],
    "Azure": GROUP_COLORS["microsoft"],
}

_FALLBACK_ACCENT = "#3987e5"


def provider_color(*, label: str | None = None, group: str | None = None) -> str:
    """Return the stable brand color for a provider group or display label."""
    if group and group in GROUP_COLORS:
        return GROUP_COLORS[group]
    if label:
        if label in PROVIDER_COLORS:
            return PROVIDER_COLORS[label]
        low = label.lower()
        for key, color in PROVIDER_COLORS.items():
            if key.lower() in low or low in key.lower():
                return color
    return _FALLBACK_ACCENT


def provider_color_map(
    labels: Iterable[str], *, groups: Iterable[str] | None = None
) -> dict[str, str]:
    """Build a Plotly ``color_discrete_map`` for a set of provider labels."""
    label_list = list(labels)
    grp_list = list(groups) if groups is not None else [None] * len(label_list)
    return {
        label: provider_color(label=label, group=grp)
        for label, grp in zip(label_list, grp_list, strict=False)
    }


def rgba_hex(hex_color: str, alpha: float) -> str:
    """Convert ``#RRGGBB`` to an ``rgba(...)`` string for Plotly fills."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def delta_variant(delta: float) -> str:
    """Map a signed dollar delta to an increase/decrease/neutral KPI variant key."""
    if delta > 0:
        return "increase"
    if delta < 0:
        return "decrease"
    return "neutral"


def md_money(v: float, *, bold: bool = True) -> str:
    """Format dollars for inline text/markdown."""
    text = f"${v:,.0f}"
    return f"**{text}**" if bold else text


def compact_money(v: float) -> str:
    """Format a dollar amount compactly for KPI cards — ``$20K`` / ``$1.2M`` / ``$950``.

    Trailing ``.0`` is trimmed so round magnitudes read clean (``$20K`` not
    ``$20.0K``); below 1,000 it falls back to a plain ``$``-grouped integer.
    """
    a = abs(v)
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= cutoff:
            scaled = f"{v / cutoff:.1f}".rstrip("0").rstrip(".")
            return f"${scaled}{suffix}"
    return f"${v:,.0f}"
