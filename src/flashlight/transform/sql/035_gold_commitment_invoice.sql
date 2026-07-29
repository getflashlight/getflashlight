-- GOLD: Contract Commitment & Invoice Details — the two FOCUS column groups this
-- pipeline previously discarded (see docs/design and CLAUDE.md's Databricks coverage
-- notes). NULL wherever a source has none (currently: Databricks — no system table
-- exposes reservation/savings-plan or invoice data; see commitment_summary_month's
-- and invoice_reconciliation_month's absence there by construction).

-- ── "Is my commitment discount going to waste?" — RI/Savings-Plan coverage ───────
-- One row per (provider, month, commitment type/category/status). commitment_count
-- is the number of distinct commitments seen, not a cost — status='Unused' rows are
-- the direct "wasted commitment" signal (paying for a reservation/plan nothing drew
-- down this month).
CREATE OR REPLACE VIEW gold.commitment_summary_month AS
SELECT
    provider_name,
    charge_month,
    commitment_discount_type,
    commitment_discount_category,
    commitment_discount_status,
    sum(cost)                                            AS effective_cost,
    sum(billed_cost)                                      AS billed_cost,
    count(DISTINCT commitment_discount_id)                AS commitment_count
FROM silver.focus_normalized
WHERE commitment_discount_id IS NOT NULL
GROUP BY provider_name, charge_month, commitment_discount_type,
    commitment_discount_category, commitment_discount_status;


-- ── "Does this tie to my invoice?" — reconciliation grain ────────────────────────
-- One row per (provider, billing account, invoice, month) — lets a consumer verify
-- GOLD's total against a specific invoice, and group a multi-invoice billing account.
CREATE OR REPLACE VIEW gold.invoice_reconciliation_month AS
SELECT
    provider_name,
    billing_account_id,
    invoice_id,
    charge_month,
    sum(billed_cost)                                      AS billed_cost
FROM silver.focus_normalized
WHERE invoice_id IS NOT NULL
GROUP BY provider_name, billing_account_id, invoice_id, charge_month;
