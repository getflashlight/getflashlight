"""The deterministic policy-compliance rule pool.

Config-driven, same convention as :mod:`waste_rules`: add a ``PolicyRule`` with
``applies_sql`` set and the next ``flashlight transform`` classifies it into
``gold.policy_record`` — no other code change. Every rule compiles to plain DuckDB
SQL (see :func:`build_policy_record_sql`), evaluated over the same
``metrics.efficiency_record`` telemetry the waste plane reads.

Unlike ``waste_record`` (only a row when a rule *fires* — a violation), every ACTIVE
rule emits one row per applicable entity every month, ``status`` = 'compliant' |
'non_compliant' | 'not_applicable' (config telemetry unmeasured for this entity) —
a real coverage denominator, not just a violations list, so "N% of clusters have
auto-termination set" is answerable directly. This is a governance/compliance
signal, not a dollar figure — see the ``efficiency/waste`` plane for recoverable $;
mixing the two would violate the waste-honesty invariant (never claim a $ figure for
a config gap that isn't itself measured waste).

Rules with ``applies_sql=None`` are BLOCKED — a real policy-compliance check that
needs telemetry this connector doesn't pull yet. They stay in the pool (visible via
the MCP ``list_policy_rules`` tool) so the roadmap is honest, but contribute no rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyRule:
    """One row of the policy-compliance pool. ACTIVE iff ``applies_sql`` is set.

    ``applies_sql``/``compliant_sql``/``not_applicable_sql``/``detail_sql`` are DuckDB
    expressions evaluated over the ``e`` CTE in :func:`build_policy_record_sql`
    (columns: the EfficiencyRecord identity fields plus the unpacked ``cause_detail``
    keys ``auto_termination_minutes``, ``min_autoscale_workers``,
    ``max_autoscale_workers`` (``interactive`` only — ephemeral job clusters aren't
    joined to cluster config, same scoping as ``waste_rules.py``), ``policy_id``
    (``interactive`` only — no warehouse counterpart), and ``tag_count``
    (``interactive``/``sql_warehouse`` — a resource-level tag count, distinct from the
    per-usage-row cost-allocation tag already read into ``owner_project``).
    """

    category: str
    label: str
    remedy: str
    applies_sql: str | None = None  # None = blocked, not yet evaluable
    compliant_sql: str = "true"
    not_applicable_sql: str = "false"
    detail_sql: str = "''"
    requires: tuple[str, ...] = ()  # telemetry needed to activate a blocked rule
    source: str = "flashlight"


POLICY_RULES: tuple[PolicyRule, ...] = (
    # ── Active: cluster-config guardrails (system.compute.clusters) ────────────────
    # Interactive-only, same scoping as waste_rules.py's cluster-config rules —
    # ephemeral per-run job clusters aren't joined to cluster_meta here.
    PolicyRule(
        category="auto_terminate",
        label="Auto-termination policy",
        remedy="Set an auto-termination timeout (e.g. 30-60 min) on this interactive "
        "cluster so idle time between sessions stops billing.",
        applies_sql="entity_type = 'interactive'",
        compliant_sql="auto_termination_minutes IS NOT NULL",
        detail_sql="CASE WHEN auto_termination_minutes IS NOT NULL "
        "THEN 'auto-terminate after ' || auto_termination_minutes || ' min' "
        "ELSE 'no auto-termination policy set' END",
    ),
    PolicyRule(
        category="autoscaling",
        label="Autoscaling configured",
        remedy="Configure an autoscale min/max worker range instead of a fixed "
        "cluster size, so it can shrink when idle and grow under load.",
        applies_sql="entity_type = 'interactive'",
        compliant_sql="min_autoscale_workers IS NOT NULL AND max_autoscale_workers IS NOT NULL "
        "AND max_autoscale_workers > min_autoscale_workers",
        detail_sql="CASE WHEN min_autoscale_workers IS NOT NULL "
        "AND max_autoscale_workers IS NOT NULL "
        "THEN 'autoscale ' || min_autoscale_workers || '-' || max_autoscale_workers || ' workers' "
        "ELSE 'fixed-size cluster, no autoscale range' END",
    ),
    PolicyRule(
        category="cluster_policy_assigned",
        label="Cluster policy assigned",
        remedy="Attach a cluster policy to enforce org-wide guardrails (node types, "
        "auto-termination, required tags) centrally instead of per-cluster config.",
        applies_sql="entity_type = 'interactive'",
        compliant_sql="policy_id IS NOT NULL",
        detail_sql="CASE WHEN policy_id IS NOT NULL THEN 'policy ' || policy_id "
        "ELSE 'no cluster policy assigned' END",
    ),
    # ── Active: tagging (system.compute.clusters/warehouses.tags) ──────────────────
    # not_applicable when tag_count itself is unmeasured (NULL) — distinct from a
    # measured, confirmed-empty tag map (tag_count = 0, non_compliant) — same honesty
    # discipline as waste_rules.py's utilization_pct IS NOT NULL gating.
    PolicyRule(
        category="cluster_tagging",
        label="Cluster tagged",
        remedy="Add cost-allocation tags (e.g. team, project, environment) to this "
        "cluster so its spend can be attributed at a granular level.",
        applies_sql="entity_type = 'interactive'",
        not_applicable_sql="tag_count IS NULL",
        compliant_sql="tag_count > 0",
        detail_sql="CASE WHEN tag_count IS NULL THEN 'tag telemetry unavailable' "
        "WHEN tag_count = 0 THEN 'no tags set' ELSE tag_count || ' tag(s) set' END",
    ),
    PolicyRule(
        category="warehouse_tagging",
        label="SQL warehouse tagged",
        remedy="Add cost-allocation tags (e.g. team, project, environment) to this "
        "SQL warehouse so its spend can be attributed at a granular level.",
        applies_sql="entity_type = 'sql_warehouse'",
        not_applicable_sql="tag_count IS NULL",
        compliant_sql="tag_count > 0",
        detail_sql="CASE WHEN tag_count IS NULL THEN 'tag telemetry unavailable' "
        "WHEN tag_count = 0 THEN 'no tags set' ELSE tag_count || ' tag(s) set' END",
    ),
    # ── Blocked: needs telemetry this connector doesn't pull yet ────────────────────
    PolicyRule(
        category="warehouse_auto_stop",
        label="SQL warehouse auto-stop policy",
        remedy="Set an auto-stop timeout on this SQL warehouse so idle time stops "
        "billing — same guardrail as cluster auto-termination.",
        requires=(
            "system.compute.warehouses.auto_stop_minutes — not yet pulled by "
            "databricks_efficiency.sql's warehouse_meta CTE",
        ),
    ),
    PolicyRule(
        category="query_tagging",
        label="Query-level tagging",
        remedy="Tag ad-hoc SQL work at the client/session level so it can be "
        "attributed as granularly as scheduled jobs.",
        requires=(
            "a per-query tag column on system.query.history — not confirmed to "
            "exist on this connector's telemetry",
        ),
    ),
)


def build_policy_record_sql() -> str:
    """Compile the ACTIVE rules into the ``gold.policy_record`` view SQL.

    Config-driven: adding a :class:`PolicyRule` with ``applies_sql`` set is picked up
    here with no other code change — the next ``flashlight transform`` classifies it.
    """
    active = [r for r in POLICY_RULES if r.applies_sql is not None]
    branches = "\nUNION ALL\n".join(
        f"""SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project,
       '{r.category}' AS policy_category,
       CASE WHEN {r.not_applicable_sql} THEN 'not_applicable'
            WHEN {r.compliant_sql} THEN 'compliant'
            ELSE 'non_compliant' END AS status,
       {r.detail_sql} AS detail
FROM e
WHERE {r.applies_sql}"""
        for r in active
    )
    return f"""
CREATE OR REPLACE VIEW gold.policy_record AS
WITH e AS (
    SELECT
        provider_name,
        strptime(charge_month, '%Y-%m')::date              AS charge_month,
        entity_type,
        entity_id,
        entity_name,
        owner_user,
        owner_project,
        TRY_CAST(json_extract_string(cause_detail, '$.auto_termination_minutes') AS BIGINT)
                                                           AS auto_termination_minutes,
        TRY_CAST(json_extract_string(cause_detail, '$.min_autoscale_workers') AS BIGINT)
                                                           AS min_autoscale_workers,
        TRY_CAST(json_extract_string(cause_detail, '$.max_autoscale_workers') AS BIGINT)
                                                           AS max_autoscale_workers,
        json_extract_string(cause_detail, '$.policy_id')  AS policy_id,
        TRY_CAST(json_extract_string(cause_detail, '$.tag_count') AS BIGINT)
                                                           AS tag_count
    FROM metrics.efficiency_record
)
{branches};
"""
