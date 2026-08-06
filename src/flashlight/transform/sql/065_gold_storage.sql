-- GOLD: the cloud storage bill behind a data platform.
--
-- WHY THIS EXISTS: Databricks' FOCUS bill (system.billing.usage) covers DBU compute only.
-- Storage lives in customer-owned S3 buckets billed by AWS, so "Databricks storage cost"
-- is invisible in the Databricks plane. Unity Catalog knows which buckets it uses; the AWS
-- FOCUS export knows what those buckets cost. This joins the two.
--
-- WHAT COUNTS — MANAGED STORAGE ONLY, and this distinction is the whole point:
--
--   * MANAGED storage — the metastore storage_root AND a MANAGED_CATALOG's storage_root:
--     Databricks provisioned it and owns the data's lifecycle — drop a managed table and
--     the bytes are deleted. That is genuinely Databricks storage cost, so it is what
--     `mapping = 'databricks'` means. Both levels count, because on a real account they sit
--     on DIFFERENT buckets (a medallion setup had bronze/silver/gold catalogs each on their
--     own bucket, none of them under the metastore root), so counting only one level
--     silently drops the other.
--   * A FOREIGN catalog (`location_kind = 'foreign_catalog'` — a Glue/Hive federation, Delta
--     Sharing, or the system catalog) is NOT managed storage even though it appears in the
--     catalog list: its storage_location points at data that already existed. Measured on a
--     real account, a single federated Glue catalog's bucket cost 5x the managed catalogs
--     combined, so lumping catalogs together by name alone would have charged another team's
--     data lake to Databricks. The connector splits the kinds at collection time.
--   * EXTERNAL locations: pre-existing S3 data registered for access. Drop the external
--     location and the data is untouched, because it was never Databricks'. The bucket
--     exists whether or not Databricks reads it, so charging it to "Databricks storage"
--     DOUBLE-CLAIMS spend that belongs to whoever owns that pipeline. Excluded — those
--     buckets fall to `mapping = 'unmapped'` through the LEFT JOIN below, deliberately.
--
-- Catalog storage roots and external locations are still recorded in gold.storage_location
-- (see location_kind): the inventory is the audit trail that answers "why isn't this bucket
-- counted?". Only the COSTING is narrowed, not the collection.
--
-- KNOWN UNDER-REPORT, accepted deliberately: per-workspace DBFS root buckets and any catalog
-- whose storage_root sits on its own bucket are ALSO Databricks-provisioned managed storage,
-- and both read as 'unmapped' here. So this figure is a FLOOR on Databricks-owned storage,
-- never a ceiling. Under-claiming beats double-claiming someone else's data lake; the
-- dashboard says so out loud rather than leaving it to be discovered. (Workspace roots are
-- reachable via the account-level AccountClient.storage.list() if that's ever revisited —
-- see docs/design/backing-storage.md.)
--
-- THE INVARIANT (CLAUDE.md, "No cross-provider cost join"): this joins AWS **cost** to
-- Databricks **metadata** — a bucket list. It never joins AWS cost to Databricks cost, and
-- nothing here writes into gold/databricks/, so databricks.monthly_bill and the Databricks
-- KPIs are untouched. Every row carries both billing_provider_name (who invoices: AWS) and
-- platform_provider_name (whose metadata claims it: Databricks) precisely so a consumer
-- can't mistake one for the other. The two bills are reported side by side, never summed.
-- Provider-facing GOLD (silver.focus_provider_bill → aws.*) excludes Amazon S3 entirely;
-- mapped rows here are named `Databricks Storage` and are that spend's only GOLD home.
--
-- HONESTY: the AWS bill's S3 ResourceId is BUCKET-grained, while a UC external location is
-- s3://bucket/prefix. A prefix-scoped location therefore shares its bucket with whatever
-- else lives there and its cost can only ever be an UPPER BOUND — that's mapping_confidence,
-- and it is never silently dropped. Every S3 row is kept (mapped, unmapped, or carrying no
-- ResourceId at all) so the mapped figure has a real denominator.

CREATE OR REPLACE VIEW gold.storage_location AS
SELECT
    provider_name                                      AS platform_provider_name,
    strptime(snapshot_month, '%Y-%m')::date            AS snapshot_month,
    location_kind,
    location_name,
    url,
    scheme,
    cloud_provider_name,
    bucket_name,
    -- NULL key_prefix means the URL addresses the bucket root. Surfacing that as a real
    -- label rather than a NULL keeps it readable in a table and greppable in MCP output.
    coalesce(key_prefix, '(bucket root)')              AS key_prefix,
    is_read_only,
    credential_name,
    x_source_connector
FROM metrics.storage_location;


CREATE OR REPLACE VIEW gold.backing_storage_month AS
WITH map_latest AS (
    -- Only the newest snapshot. The map is current UC state applied to every month of
    -- cost, so a bucket registered last week is credited with its earlier cost too —
    -- usually right (the data predates the registration) but an assumption, which is why
    -- the dashboard caption says the map is "as of the last sync". Earlier snapshots stay
    -- in metrics.storage_location as the audit trail.
    SELECT *
    FROM metrics.storage_location
    WHERE snapshot_month = (SELECT max(snapshot_month) FROM metrics.storage_location)
),
bucket_map AS (
    -- EXACTLY ONE ROW PER BUCKET. Load-bearing, not tidiness: a metastore root can be
    -- reported more than once (several metastores, or a re-pull mid-window), and joining
    -- raw location rows would multiply that bucket's cost by the number of locations —
    -- inventing spend that isn't on the bill.
    --
    -- The IN-list is the managed-storage filter described in the header. It also gives
    -- managed storage PRECEDENCE for free: a bucket that is both managed and an external
    -- location target (common — a catalog's storage_root often sits inside an external
    -- location's path) still matches here, so it reads 'databricks' rather than being
    -- demoted by its external-location row.
    SELECT
        bucket_name,
        any_value(provider_name)                       AS platform_provider_name,
        -- Any location addressing the bucket root means the whole bucket is platform
        -- storage; otherwise UC only claims prefixes and the bill can't be split.
        bool_or(key_prefix IS NULL)                    AS has_bucket_root_location,
        count(*)                                       AS location_count,
        -- WHICH Unity Catalog object owns this bucket, so cost can be read per catalog.
        -- A metastore root wins when a bucket carries both (it's the broader container, and
        -- the bill can't be split between them anyway). When several catalogs share ONE
        -- bucket the name is deliberately withheld rather than picking one arbitrarily —
        -- the AWS bill is bucket-grained, so per-catalog cost genuinely isn't knowable
        -- there, and naming one would silently attribute its neighbours' bytes to it.
        CASE
            WHEN bool_or(location_kind = 'metastore_root')
                THEN min(location_name) FILTER (WHERE location_kind = 'metastore_root')
            WHEN count(DISTINCT location_name) = 1 THEN any_value(location_name)
            ELSE '(shared by ' || count(DISTINCT location_name) || ' catalogs)'
        END                                            AS managed_name,
        CASE WHEN bool_or(location_kind = 'metastore_root')
             THEN 'metastore_root' ELSE 'catalog' END  AS managed_kind
    FROM map_latest
    WHERE cloud_provider_name = 'AWS'
      AND bucket_name IS NOT NULL
      AND location_kind IN ('metastore_root', 'catalog')
    GROUP BY bucket_name
),
s3_cost AS (
    SELECT
        provider_name,
        service_name,
        region_id,
        charge_month,
        cost,
        is_credit,
        is_partial_period,
        -- Unlike gold.spend_by_cost_subcategory_month, an unclassified row must NOT be
        -- dropped here: it is part of the denominator, and an existing lake ingested
        -- before the S3 classifier shipped has NULL for every S3 row.
        coalesce(x_cost_subcategory, '(unclassified)') AS cost_subcategory,
        -- A real FOCUS export carries S3's ResourceId as the bucket ARN
        -- (arn:aws:s3:::name); fall back to a bare bucket name, which is what older
        -- exports and the repo's own fixtures/seeded demo lake carry.
        nullif(
            coalesce(
                nullif(regexp_extract(resource_id, '^arn:aws:s3:::([^/]+)', 1), ''),
                resource_id
            ),
            ''
        )                                              AS bucket_name
    FROM silver.focus_normalized
    WHERE provider_name = 'AWS'
      AND service_name = 'Amazon Simple Storage Service'
)
SELECT
    c.provider_name                                    AS billing_provider_name,
    -- Mapped rows are Databricks Storage at transform time — not Amazon S3 under AWS
    -- (provider GOLD excludes S3 entirely; this plane is their only GOLD home).
    CASE WHEN m.bucket_name IS NOT NULL THEN 'Databricks Storage' ELSE c.service_name END
                                                       AS service_name,
    coalesce(c.bucket_name, '(no resource id)')        AS bucket_name,
    CASE
        WHEN c.bucket_name IS NULL     THEN 'no_resource_id'
        WHEN m.bucket_name IS NOT NULL THEN 'databricks'
        ELSE 'unmapped'
    END                                                AS mapping,
    m.platform_provider_name,
    -- Which Unity Catalog object's storage this is: a catalog name, the metastore name, or
    -- '(not managed)' for every unmapped row. Group by this to read cost per catalog.
    coalesce(m.managed_name, '(not managed)')          AS managed_name,
    coalesce(m.managed_kind, 'n/a')                    AS managed_kind,
    CASE
        WHEN m.bucket_name IS NULL      THEN 'n/a'
        WHEN m.has_bucket_root_location THEN 'whole_bucket'
        ELSE 'prefix_scoped'
    END                                                AS mapping_confidence,
    c.cost_subcategory,
    coalesce(c.region_id, '(none)')                    AS region_id,
    c.charge_month,
    coalesce(max(m.location_count), 0)                 AS location_count,
    sum(c.cost)                                        AS net_cost,
    sum(c.cost) FILTER (WHERE NOT c.is_credit)         AS gross_cost,
    bool_or(c.is_partial_period)                       AS is_partial_period
FROM s3_cost c
LEFT JOIN bucket_map m ON m.bucket_name = c.bucket_name
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;
