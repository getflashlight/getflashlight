-- GOLD: the policy-compliance contract. ONE consumer view; the dashboard and MCP read it.
--
-- gold.policy_record itself is generated at transform time, not defined here — see
-- flashlight.efficiency.policy_rules.build_policy_record_sql(). Classification is a
-- deterministic, config-driven pool of PolicyRule entries (plain DuckDB SQL per rule,
-- no LLM/skill judgment) so a new rule is picked up on the next `flashlight transform`
-- with no SQL edit. Unlike gold.waste_record, every ACTIVE rule emits one row per
-- applicable entity every month (status compliant/non_compliant/not_applicable) — a
-- real coverage denominator, not just a violations list. No dollar figure: this is a
-- governance/compliance signal, not spend/waste (see the efficiency/waste plane for $).

-- ── KPI rollup: entity count per month × category × status ──────────────────────────
CREATE OR REPLACE VIEW gold.policy_summary_month AS
SELECT
    charge_month,
    policy_category,
    status,
    count(*)                                               AS entity_count
FROM gold.policy_record
GROUP BY charge_month, policy_category, status;
