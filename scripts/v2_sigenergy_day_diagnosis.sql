-- Which stored Sigenergy days may be wrong, and why. Read-only, on purpose.
--
-- Two defects wrote into `production_facts` before they were fixed, and
-- neither can be undone from code alone:
--
--   * the day still in progress was closed as a daily total, so the stored
--     value is whatever the cumulative counter held at collection time;
--   * a day whose payload was empty or partial was accepted, so the cursor
--     moved past readings that were never collected.
--
-- Repairing them means re-reading those days from the provider and letting
-- `record_production_fact` supersede the wrong values with a new revision.
-- That is a decision with a provider budget attached, so this script only
-- names the days. It deletes nothing, updates nothing, and is safe to run
-- against production.
--
--   psql -U nemsei -d nemsei_v2 -f scripts/v2_sigenergy_day_diagnosis.sql
--
BEGIN;
SET TRANSACTION READ ONLY;

\echo '== 1. days closed while the source day was still open =='
-- The run that produced the fact started *inside* the day the fact covers.
-- For a cumulative daily counter that is a mid-day reading stored as a total.
SELECT
    c.connection_key,
    f.asset_id,
    f.provider_mapping_id,
    (f.period_start AT TIME ZONE COALESCE(f.metadata_json ->> 'source_timezone', 'UTC'))::date AS source_day,
    f.metric_kind,
    f.value,
    f.source_revision,
    r.started_at AS collected_at
FROM production_facts f
JOIN asset_provider_mappings m ON m.id = f.provider_mapping_id
JOIN provider_connections c ON c.id = m.provider_connection_id
JOIN sync_runs r ON r.id = f.sync_run_id
WHERE c.provider_code = 'sigenergy'
  AND r.started_at >= f.period_start
  AND r.started_at < f.period_end
ORDER BY source_day DESC, f.asset_id, f.metric_kind;

\echo ''
\echo '== 2. stored days with no reading at all =='
-- A `missing` fact is honest evidence that the day was asked for and came
-- back empty. It becomes a problem only where coverage moved past it anyway,
-- which section 3 answers.
SELECT
    c.connection_key,
    f.asset_id,
    (f.period_start AT TIME ZONE COALESCE(f.metadata_json ->> 'source_timezone', 'UTC'))::date AS source_day,
    count(*) FILTER (WHERE f.quality = 'missing') AS missing_metrics,
    count(*) AS metrics
FROM production_facts f
JOIN asset_provider_mappings m ON m.id = f.provider_mapping_id
JOIN provider_connections c ON c.id = m.provider_connection_id
WHERE c.provider_code = 'sigenergy'
  AND f.granularity = 'day'
GROUP BY 1, 2, 3
HAVING count(*) FILTER (WHERE f.quality = 'missing') > 0
ORDER BY source_day DESC, f.asset_id;

\echo ''
\echo '== 3. days coverage claims but no complete reading exists =='
-- The gap the cursor is hiding: every source day up to `last_completed_day`
-- that has no complete production reading for a mapping that was active then.
WITH cursors AS (
    SELECT
        sc.provider_connection_id,
        (sc.checkpoint_json ->> 'last_completed_day')::date AS covered_through,
        COALESCE(sc.checkpoint_json ->> 'source_timezone', 'UTC') AS zone
    FROM sync_cursors sc
    JOIN provider_connections c ON c.id = sc.provider_connection_id
    WHERE c.provider_code = 'sigenergy'
      AND sc.cursor_key = 'sigenergy-daily-production'
      AND sc.checkpoint_json ->> 'last_completed_day' IS NOT NULL
),
obligations AS (
    SELECT m.id AS mapping_id, m.asset_id, cu.provider_connection_id, day::date AS source_day
    FROM cursors cu
    JOIN asset_provider_mappings m
      ON m.provider_connection_id = cu.provider_connection_id
     AND m.mapping_status = 'active'
     AND m.resource_kind = 'plant'
    CROSS JOIN LATERAL generate_series(
        GREATEST(m.valid_from, cu.covered_through - INTERVAL '120 days')::date,
        LEAST(COALESCE(m.valid_to, cu.covered_through), cu.covered_through)::date,
        INTERVAL '1 day'
    ) AS day
)
SELECT o.provider_connection_id, o.asset_id, o.mapping_id, o.source_day
FROM obligations o
WHERE NOT EXISTS (
    SELECT 1
    FROM production_facts f
    WHERE f.provider_mapping_id = o.mapping_id
      AND f.metric_kind = 'production_energy'
      AND f.granularity = 'day'
      AND f.quality = 'complete'
      AND f.value IS NOT NULL
      AND (f.period_start AT TIME ZONE COALESCE(f.metadata_json ->> 'source_timezone', 'UTC'))::date = o.source_day
)
ORDER BY o.source_day DESC, o.asset_id;

\echo ''
\echo '== 4. summary =='
SELECT
    c.connection_key,
    count(*) FILTER (WHERE f.quality = 'complete') AS complete_facts,
    count(*) FILTER (WHERE f.quality = 'partial') AS partial_facts,
    count(*) FILTER (WHERE f.quality = 'missing') AS missing_facts,
    min(f.period_start) AS earliest,
    max(f.period_start) AS latest
FROM production_facts f
JOIN asset_provider_mappings m ON m.id = f.provider_mapping_id
JOIN provider_connections c ON c.id = m.provider_connection_id
WHERE c.provider_code = 'sigenergy'
GROUP BY 1
ORDER BY 1;

ROLLBACK;
