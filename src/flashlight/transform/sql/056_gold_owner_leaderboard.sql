-- GOLD: the "who owns this waste?" contract. ONE consumer view; the dashboard and MCP read it.
--
-- Ranks recoverable spend by owner. Two owner dimensions in one view (owner_dimension =
-- 'owner_user' | 'owner_project') rather than two views, so the coverage gap between them is
-- a single GROUP BY away — and it is a big gap worth seeing: on a real lake owner_user is
-- populated on ~94% of findings while owner_project is on ~1%.
--
-- WHY THE NORMALIZATION LIVES HERE, not in the dashboard. Raw owner_user is dirty in three
-- ways that all split one human across several rows: casing, trailing whitespace (Redshift
-- CHAR padding), and service principals arriving as bare UUIDs. If the dashboard folded
-- those and MCP did not, then `query_view('efficiency.waste_by_owner_month')` and the
-- provider page's Attribution tab would rank owners differently — two answers to one question,
-- which is exactly what the "dashboard and agents always agree" invariant forbids. So the
-- fold is baked into published GOLD, the same way policies.yml thresholds are.
--
-- ATTRIBUTION HONESTY. owner_key is NEVER NULL: unowned findings collapse into a literal
-- '(unattributed)' row instead of vanishing. This matters more than it looks — on a real
-- lake that single row is the LARGEST bucket (~$143k across ~420 sql_warehouse findings),
-- because shared compute has no owner *by design* (see efficiency/model.py's EntityType
-- docs), not because data is missing. A `WHERE owner_user IS NOT NULL` would silently drop
-- the biggest number on the page.
--
-- Never sum across `lens`: WASTE (tune it) and OPPORTUNITY (move it) are different remedies
-- and adding them together invents a headline number that means nothing.
CREATE OR REPLACE VIEW gold.waste_by_owner_month AS
WITH u AS (
    SELECT
        provider_name,
        charge_month,
        lens,
        confidence,
        entity_id,
        billed_cost,
        recoverable_cost,
        -- Fold case and padding together: 'Alice ', 'alice' and 'ALICE' are one owner.
        -- nullif('') so an empty-string owner is treated as absent, not as a named owner
        -- whose name happens to be blank.
        nullif(trim(owner_user), '')                            AS owner_raw,
        lower(nullif(trim(owner_user), ''))                     AS owner_folded
    FROM gold.waste_record
),
u_agg AS (
    SELECT
        provider_name,
        charge_month,
        'owner_user'                                           AS owner_dimension,
        lens,
        coalesce(owner_folded, '(unattributed)')                AS owner_key,
        CASE WHEN owner_folded IS NULL THEN 'unattributed_shared_compute'
             -- Bare 8-4-4-4-12 UUIDs are Databricks service principals, not people. They
             -- routinely top the leaderboard, so labelling them keeps a human from
             -- hunting for a colleague who does not exist.
             WHEN regexp_full_match(
                 owner_folded,
                 '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
                 THEN 'service_principal'
             ELSE 'user' END                                    AS owner_kind,
        -- The spelling that appeared on this owner's costliest finding — the folded key is
        -- for grouping, this is for reading.
        max_by(owner_raw, recoverable_cost)                     AS owner_raw_display,
        sum(recoverable_cost)                                   AS recoverable_cost,
        sum(recoverable_cost) FILTER (WHERE confidence = 'high')
                                                               AS recoverable_cost_high_confidence,
        sum(billed_cost)                                        AS billed_cost,
        count(DISTINCT entity_id)                               AS entity_count,
        count(*)                                                AS finding_count
    FROM u
    GROUP BY provider_name, charge_month, lens, owner_key, owner_kind
),
p AS (
    SELECT
        provider_name,
        charge_month,
        lens,
        confidence,
        entity_id,
        billed_cost,
        recoverable_cost,
        nullif(trim(owner_project), '')                        AS owner_raw,
        lower(nullif(trim(owner_project), ''))                 AS owner_folded
    FROM gold.waste_record
),
p_agg AS (
    SELECT
        provider_name,
        charge_month,
        'owner_project'                                        AS owner_dimension,
        lens,
        coalesce(owner_folded, '(unattributed)')                AS owner_key,
        -- No service principals on the project side: a project tag is a free-text label,
        -- never an identity, so there are only two kinds here.
        CASE WHEN owner_folded IS NULL THEN 'unattributed'
             ELSE 'project' END                                 AS owner_kind,
        max_by(owner_raw, recoverable_cost)                     AS owner_raw_display,
        sum(recoverable_cost)                                   AS recoverable_cost,
        sum(recoverable_cost) FILTER (WHERE confidence = 'high')
                                                               AS recoverable_cost_high_confidence,
        sum(billed_cost)                                        AS billed_cost,
        count(DISTINCT entity_id)                               AS entity_count,
        count(*)                                                AS finding_count
    FROM p
    GROUP BY provider_name, charge_month, lens, owner_key, owner_kind
),
combined AS (
    SELECT * FROM u_agg
    UNION ALL
    SELECT * FROM p_agg
)
-- owner_display is applied in an outer wrapper because it depends on max_by, an aggregate.
SELECT
    provider_name,
    charge_month,
    owner_dimension,
    lens,
    owner_key,
    owner_kind,
    CASE owner_kind
        WHEN 'unattributed_shared_compute' THEN 'Unattributed (shared compute)'
        WHEN 'unattributed'                THEN 'Unattributed'
        -- 8 hex chars is enough to grep the source system for; the full 36 are unreadable
        -- in a table cell. owner_key still carries the whole UUID for exact agent filters.
        WHEN 'service_principal'           THEN 'Service principal ' || left(owner_key, 8)
        ELSE coalesce(owner_raw_display, owner_key)
    END                                                         AS owner_display,
    recoverable_cost,
    recoverable_cost_high_confidence,
    billed_cost,
    entity_count,
    finding_count
FROM combined;
