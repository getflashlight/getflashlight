"""Backing storage — the AWS-billed S3 cost of Databricks-managed storage.

Databricks' own bill (``system.billing.usage``) covers DBU compute only; storage lives
in customer-owned S3 buckets billed by AWS. This tab reports the cost of the buckets
Databricks **manages**, so "what does Databricks storage cost?" has an answer at all.

**The rule this whole module exists to hold**: these dollars are billed by AWS and are
already counted in ``aws.monthly_bill``. They are never added to Databricks spend —
that sum is the TCO join CLAUDE.md removed, and nothing here recreates it. Two bills,
reported side by side.

**Managed only.** ``mapping='databricks'`` means the bucket holds the Unity Catalog
metastore root — storage Databricks provisioned and whose lifecycle it owns. External
locations are excluded: that data pre-existed and is only registered for access, so
counting it would double-claim spend belonging to whoever owns that pipeline. See
``065_gold_storage.sql``'s header and ``docs/design/backing-storage.md``.

Two honesty constraints still shape the panels:

* The AWS bill's S3 ``ResourceId`` is **bucket**-grained while a metastore root is
  normally ``s3://bucket/<metastore-id>``. A prefix-scoped root shares its bucket with
  whatever else lives there, so its cost is an **upper bound** on Databricks' share —
  still reported as the bucket's billed figure (GOLD keeps ``mapping_confidence`` for
  consumers that need the distinction).
* Absence of a mapping is not absence of Databricks storage cost. Every empty state
  names its own cause (no map / no S3 rows / map but nothing managed) rather than going
  blank.

The prose that used to lead this tab (the two-bills rule, the floor disclosure, the
managed-share coverage line and the gap list) was removed at the user's request — four
paragraphs above the first number. The rules themselves live in this docstring, in
``065_gold_storage.sql``'s header and in ``docs/design/backing-storage.md``; the figure
is still a **floor**, and per-workspace DBFS roots and per-catalog storage roots are
still uncounted, the tab just no longer says it on screen.

Deliberately **not** here: a per-bucket list of everything unmapped. Non-managed buckets
aren't this tab's subject, and with thousands of them on a real account that table
buried the one number the tab exists to report. Per-bucket detail stays queryable in
GOLD/MCP.

One table, not two: the managed buckets and the Unity Catalog objects that own them were
separate panels showing the same rows twice (each managed object sits on its own bucket),
so they are merged — bucket, owner and kind on one line, with cost precision on the
dollar figure rather than a jargon column.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.theme import compact_money

_GROUP = "storage"
_VIEW = "backing_storage_month"

#: Rendered when there is no storage map at all. Says outright what absence does NOT mean.
NO_MAP_MSG = (
    "No Unity Catalog storage map yet — run `flashlight ingest` with a Databricks "
    "connection configured. Absence of a mapping is not absence of Databricks storage "
    "cost: it means nothing has looked yet."
)

#: Map present, but no S3 rows in the AWS bill to label.
NO_S3_COST_MSG = (
    "No Amazon S3 rows in the AWS bill for this lake. Add `Amazon Simple Storage "
    "Service` to this connection's `include_services` in connections.yml (it is in the "
    "default), then re-run `flashlight ingest`."
)

#: S3 cost present, but nothing managed — the case most likely to be misread as
#: "Databricks has no storage cost".
NO_LOCATIONS_MSG = (
    "Found S3 spend but no Databricks-managed storage. Either the metadata pull has not "
    "run, or the token cannot read the Unity Catalog metastore summary — that grant is "
    "what identifies the metastore root, and without it every bucket looks unmanaged. "
    "This does not mean Databricks has no storage cost."
)

_SUBCATEGORY_LABELS = {
    "storage": "Storage (bytes stored)",
    "requests": "Requests & retrievals",
    "data_transfer": "Data transfer",
    "monitoring": "Intelligent-Tiering monitoring",
    "early_delete": "Early-delete fees",
    "other": "Other S3 charges",
    "(unclassified)": "Unclassified",
}


def _df(sql: str) -> pd.DataFrame:
    """Query GOLD, returning empty on any issue (the view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render an empty state
        return pd.DataFrame()


def kpi_card(sm: date, end: date) -> chrome.KpiCard | None:
    """This window's Databricks-managed S3 cost, as a card on the Databricks KPI row.

    **Beside the Databricks bill, never inside it.** A card in that row is the same
    side-by-side reporting this whole tab does — what it must never become is a term in
    the ``Net Spend`` total, which stays purely DBU compute (the AWS group's
    ``monthly_bill`` is where these dollars are actually billed, and they are counted
    there exactly once). The card takes its own hue rather than the provider accent so
    it doesn't read as another slice of one total; no subtitle lecture — the colour and
    the title do that work.

    Returns ``None`` — no card at all — when there is nothing to report. A "$0" card
    would say "Databricks storage is free", which is the single misreading this module
    exists to prevent: an unmapped or unpulled metastore is unmeasured, not zero. The
    tab's own empty states name which of those it is.
    """
    if not gold_view_published(_GROUP, _VIEW):
        return None
    df = _df(
        f"SELECT sum(net_cost) AS c FROM {_GROUP}.{_VIEW} WHERE mapping = 'databricks' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    if df.empty or pd.isna(df["c"].iloc[0]) or not float(df["c"].iloc[0]):
        return None
    return ("Databricks Storage", compact_money(float(df["c"].iloc[0])), "", "volume")


def render(sm: date | None = None, end: date | None = None) -> None:
    """Databricks-managed S3 cost for the page date range.

    ``sm``/``end`` come from ``provider_focus``'s after_breakdown hook so this tab
    shares the page date control. Omitted only for direct calls (tests).
    """
    chrome.section_title("Databricks Storage (billed by AWS)")

    if not gold_view_published(_GROUP, _VIEW):
        ui.label(NO_MAP_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    where = ""
    if sm is not None and end is not None:
        where = f" WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    rows = _df(
        "SELECT bucket_name, mapping, mapping_confidence, managed_name, managed_kind, "
        f"cost_subcategory, charge_month, net_cost FROM {_GROUP}.{_VIEW}{where}"
    )
    if rows.empty:
        ui.label(NO_S3_COST_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    mapped = rows[rows["mapping"] == "databricks"]
    if mapped.empty:
        # No unmapped bucket list here on purpose: with nothing managed, a table of every
        # unrelated S3 bucket is what makes this read as "all your S3 spend". The
        # denominator is still stated, as one line.
        ui.label(NO_LOCATIONS_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        _denominator_caption(rows)
        return

    _managed_storage(mapped)
    _subcategory_trend(mapped)


#: Metastore roots first, then catalogs — the root is the broader container, so it reads
#: top-down from the metastore to the catalogs underneath it.
_KIND_ORDER = {"metastore_root": 0, "catalog": 1}

_KIND_LABELS = {"metastore_root": "Metastore root", "catalog": "Catalog"}


def _qualified_cost(cost: float, name: object) -> str:
    """Format the bucket's AWS-billed S3 cost.

    A shared-catalog bucket gets ``(shared)`` because the cost can't be split across the
    catalogs that own it. Everything else is a plain ``$…`` — including ``prefix_scoped``
    upper bounds (the confidence stays in GOLD for MCP/agents; the table does not prefix
    ``≤``).
    """
    money = f"${float(cost):,.0f}"
    if str(name).startswith("(shared by"):
        return f"{money} (shared)"
    return money


def _managed_storage(mapped: pd.DataFrame) -> None:
    """One row per Databricks-managed bucket, with the Unity Catalog object that owns it.

    Bucket and owner are one table because they are one fact: each managed object sits on
    its own bucket, which is what makes bucket-grained AWS cost equal object-grained cost.
    Where it doesn't, the view hands back ``(shared by N catalogs)`` instead of a name and
    the cost is marked shared rather than attributed to one catalog.
    """
    per_bucket = (
        mapped.groupby(
            ["bucket_name", "managed_name", "managed_kind", "mapping_confidence"], as_index=False
        )["net_cost"]
        .sum()
        .sort_values("net_cost", ascending=False)
    )
    if per_bucket.empty:
        return
    per_bucket["net_cost"] = per_bucket["net_cost"].astype(float)
    per_bucket["Kind"] = per_bucket["managed_kind"].map(_KIND_LABELS)
    per_bucket["S3 cost (AWS-billed)"] = [
        _qualified_cost(cost, name)
        for cost, name in zip(
            per_bucket["net_cost"],
            per_bucket["managed_name"],
            strict=True,
        )
    ]
    per_bucket = per_bucket.assign(
        _kind_rank=per_bucket["managed_kind"].map(_KIND_ORDER).fillna(len(_KIND_ORDER))
    ).sort_values(["_kind_rank", "net_cost"], ascending=[True, False], ignore_index=True)
    chrome.panel_title("Databricks-managed storage")
    chrome.searchable_table(
        per_bucket[["managed_name", "Kind", "bucket_name", "S3 cost (AWS-billed)"]],
        key="backing_storage_managed",
        search_col="bucket_name",
        rename={
            "managed_name": "Catalog / metastore",
            "bucket_name": "Bucket",
        },
    )


def _denominator_caption(rows: pd.DataFrame) -> None:
    """The total S3 bill this tab is a subset of — kept even when nothing is managed, so
    the empty state still says how much S3 spend exists and how many buckets carry it."""
    total = float(rows["net_cost"].sum())
    buckets = _real_buckets(rows)
    chrome.section_caption(
        f"For scale: {compact_money(total)} of Amazon S3 spend across {len(buckets):,} "
        "bucket(s) in this window, none of it currently identified as Databricks-managed. "
        "Per-bucket detail is available via MCP (storage.backing_storage_month)."
    )


def _subcategory_trend(mapped: pd.DataFrame) -> None:
    """Monthly cost split by what the money actually bought.

    The split is the point: Databricks drives heavy LIST/GET metadata traffic, so a
    request-volume problem and a storage-growth problem look identical in one total but
    have completely different remedies.

    A stacked chart with no legend is a chart of unexplained colours, so ``has_legend``
    is on. ``category_x`` is on for the same reason every other monthly bar chart here
    sets it: a "YYYY-MM" string keeps Plotly from auto-detecting a date axis and placing
    its ticks between the bars (it was labelling the June bar "May 31").
    """
    by_month = (
        mapped.groupby(["charge_month", "cost_subcategory"], as_index=False)["net_cost"]
        .sum()
        .sort_values("charge_month")
    )
    if by_month.empty:
        return
    by_month["net_cost"] = by_month["net_cost"].astype(float)
    by_month["month"] = pd.to_datetime(by_month["charge_month"]).dt.strftime("%Y-%m")
    by_month["Charge type"] = by_month["cost_subcategory"].map(
        lambda s: _SUBCATEGORY_LABELS.get(str(s), str(s))
    )
    chrome.panel_title("Databricks-managed S3 cost by charge type")
    fig = px.bar(
        by_month,
        x="month",
        y="net_cost",
        color="Charge type",
        color_discrete_sequence=list(chrome.CATEGORICAL_SLOTS),
        barmode="stack",
        labels={"month": "", "net_cost": "", "Charge type": ""},
    )
    chrome.plot(chrome.style_fig(fig, currency_axis="y", has_legend=True, category_x=True))


def _real_buckets(df: pd.DataFrame) -> set[str]:
    """Distinct bucket names, excluding the ``(no resource id)`` placeholder — it is a
    cost bucket in the view, not a real S3 bucket, and counting it would inflate the
    denominator with something that can never be mapped."""
    return {str(b) for b in df["bucket_name"].unique() if str(b) != "(no resource id)"}
