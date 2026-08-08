"""Total Databricks footprint — DBU spend plus the AWS-billed infra behind it, combined
into one explicitly-labelled card.

**This is the one place those two bills are ever added together, and it says so.**
``Net Spend`` (``provider_focus.render``'s own KPI, reading ``databricks.monthly_bill``)
stays the literal Databricks invoice — DBU only — exactly as CLAUDE.md's "No
cross-provider cost join" requires, and ``backing_storage.kpi_card`` /
``backing_compute.kpi_card`` stay separate, individually-labelled cards for the same
reason. This card is additive on top of all three, never a replacement for any of them:
it names its own two components in the subtitle (DBU vs AWS infra) so a reader can never
mistake it for a bigger Databricks invoice — it's a sum across two vendors' bills, offered
because "what does running Databricks actually cost me, including the cloud VMs and
buckets behind it" is a real question, just not the same question ``Net Spend`` answers.

Omitted, never rendered with a $0 AWS-infra component, when neither backing plane has any
mapped spend for the window — at that point this card would be identical to ``Net Spend``
and adds nothing.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.theme import compact_money


def _mapped_sum(group: str, view: str, sm: date, end: date) -> float:
    if not gold_view_published(group, view):
        return 0.0
    try:
        df = gold_df(
            f"SELECT coalesce(sum(net_cost), 0) AS c FROM {group}.{view} "
            f"WHERE mapping = 'databricks' AND charge_month >= '{sm}' AND charge_month <= '{end}'"
        )
    except Exception:  # noqa: BLE001 - missing/stale view must not take the page down
        return 0.0
    return float(df["c"].iloc[0]) if not df.empty and not pd.isna(df["c"].iloc[0]) else 0.0


def footprint_card(sm: date, end: date) -> chrome.KpiCard | None:
    """DBU net + Databricks-managed Backing storage + Backing compute, for the window.

    Distinct hue from both ``Net Spend`` (default/savings-green) and the individual
    Backing storage/Backing compute cards (``"volume"``) — this is a third kind of
    number (a cross-vendor rollup), not a slice of either.
    """
    if not gold_view_published("databricks", "monthly_bill"):
        return None
    dbu_df = gold_df(
        "SELECT coalesce(sum(net_cost), 0) AS c FROM databricks.monthly_bill "
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    dbu = 0.0
    if not dbu_df.empty and not pd.isna(dbu_df["c"].iloc[0]):
        dbu = float(dbu_df["c"].iloc[0])

    storage = _mapped_sum("storage", "backing_storage_month", sm, end)
    compute = _mapped_sum("compute", "backing_compute_month", sm, end)
    aws_infra = storage + compute
    if not aws_infra:
        # Identical to Net Spend with nothing to add — a card here would just duplicate
        # it, and duplicating it right beside "Net Spend" is exactly the kind of visual
        # noise that invites someone to assume the two mean different things.
        return None

    total = dbu + aws_infra
    sub = "Includes Databricks usage and AWS infrastructure"
    return ("Total cost of ownership", compact_money(total), sub, "unattributed")
