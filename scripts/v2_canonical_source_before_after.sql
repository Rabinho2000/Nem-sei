-- What changes when production totals stop adding two sources for one day.
--
-- The readers used to reduce `production_facts` to its newest revision per
-- mapping and then sum whatever was left. An asset read by a primary and a
-- fallback therefore had both readings for the same day added together. The
-- reduction now also picks one source -- the one the source policy selects --
-- so some totals go **down**, and going down is the correct outcome.
--
-- This script shows exactly which, before anything is believed about the new
-- numbers. Read-only: it computes both answers and diffs them. The CTE block
-- is repeated per query rather than made a temp view, because a temp view is
-- a write and this must be safe to run against production.
--
--   psql -U nemsei -d nemsei_v2 -f scripts/v2_canonical_source_before_after.sql
--
BEGIN;
SET TRANSACTION READ ONLY;

\echo '== 1. how many asset-periods lose a duplicate source =='
WITH current_revision AS (
    SELECT DISTINCT ON (f.provider_mapping_id, f.source_fact_key)
           f.id AS fact_id, f.asset_id, f.provider_mapping_id,
           f.period_start, f.period_end, f.value, f.metadata_json
    FROM production_facts f
    WHERE f.metric_kind = 'production_energy'
    ORDER BY f.provider_mapping_id, f.source_fact_key, f.source_revision DESC
),
-- The chosen source per fact, ranked exactly as monitoring.repository
-- .canonical_facts ranks it: policy primary, then policy fallback, then a
-- mapping with no policy for that day.
scored AS (
    SELECT DISTINCT ON (c.fact_id)
           c.fact_id, c.asset_id, c.provider_mapping_id, c.period_start, c.period_end,
           c.value, p.is_fallback, p.priority
    FROM current_revision c
    LEFT JOIN asset_source_policies p
           ON p.asset_id = c.asset_id
          AND p.source_use = 'production'
          AND p.provider_mapping_id = c.provider_mapping_id
          AND p.valid_from <= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start))
          AND (p.valid_to IS NULL
               OR p.valid_to >= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start)))
    ORDER BY c.fact_id, p.is_fallback ASC NULLS LAST, p.priority ASC NULLS LAST, p.id ASC NULLS LAST
),
canonical AS (
    SELECT DISTINCT ON (s.asset_id, s.period_start, s.period_end) s.*
    FROM scored s
    ORDER BY s.asset_id, s.period_start, s.period_end,
             s.is_fallback ASC NULLS LAST, s.priority ASC NULLS LAST, s.provider_mapping_id ASC
)
SELECT
    (SELECT count(*) FROM current_revision WHERE value IS NOT NULL) AS rows_before,
    (SELECT count(*) FROM canonical WHERE value IS NOT NULL) AS rows_after,
    (SELECT count(*) FROM current_revision WHERE value IS NOT NULL)
      - (SELECT count(*) FROM canonical WHERE value IS NOT NULL) AS rows_dropped;

\echo ''
\echo '== 2. per-asset totals that change =='
WITH current_revision AS (
    SELECT DISTINCT ON (f.provider_mapping_id, f.source_fact_key)
           f.id AS fact_id, f.asset_id, f.provider_mapping_id,
           f.period_start, f.period_end, f.value, f.metadata_json
    FROM production_facts f
    WHERE f.metric_kind = 'production_energy'
    ORDER BY f.provider_mapping_id, f.source_fact_key, f.source_revision DESC
),
-- The chosen source per fact, ranked exactly as monitoring.repository
-- .canonical_facts ranks it: policy primary, then policy fallback, then a
-- mapping with no policy for that day.
scored AS (
    SELECT DISTINCT ON (c.fact_id)
           c.fact_id, c.asset_id, c.provider_mapping_id, c.period_start, c.period_end,
           c.value, p.is_fallback, p.priority
    FROM current_revision c
    LEFT JOIN asset_source_policies p
           ON p.asset_id = c.asset_id
          AND p.source_use = 'production'
          AND p.provider_mapping_id = c.provider_mapping_id
          AND p.valid_from <= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start))
          AND (p.valid_to IS NULL
               OR p.valid_to >= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start)))
    ORDER BY c.fact_id, p.is_fallback ASC NULLS LAST, p.priority ASC NULLS LAST, p.id ASC NULLS LAST
),
canonical AS (
    SELECT DISTINCT ON (s.asset_id, s.period_start, s.period_end) s.*
    FROM scored s
    ORDER BY s.asset_id, s.period_start, s.period_end,
             s.is_fallback ASC NULLS LAST, s.priority ASC NULLS LAST, s.provider_mapping_id ASC
),
before AS (
    SELECT asset_id, sum(value) AS total FROM current_revision WHERE value IS NOT NULL GROUP BY 1
),
after AS (
    SELECT asset_id, sum(value) AS total FROM canonical WHERE value IS NOT NULL GROUP BY 1
)
SELECT b.asset_id, a2.canonical_name,
       b.total AS total_before, COALESCE(af.total, 0) AS total_after,
       COALESCE(af.total, 0) - b.total AS delta
FROM before b
LEFT JOIN after af USING (asset_id)
LEFT JOIN assets a2 ON a2.id = b.asset_id
WHERE COALESCE(af.total, 0) <> b.total
ORDER BY abs(COALESCE(af.total, 0) - b.total) DESC;

\echo ''
\echo '== 3. the days where two sources were being added =='
WITH current_revision AS (
    SELECT DISTINCT ON (f.provider_mapping_id, f.source_fact_key)
           f.id AS fact_id, f.asset_id, f.provider_mapping_id,
           f.period_start, f.period_end, f.value, f.metadata_json
    FROM production_facts f
    WHERE f.metric_kind = 'production_energy'
    ORDER BY f.provider_mapping_id, f.source_fact_key, f.source_revision DESC
),
-- The chosen source per fact, ranked exactly as monitoring.repository
-- .canonical_facts ranks it: policy primary, then policy fallback, then a
-- mapping with no policy for that day.
scored AS (
    SELECT DISTINCT ON (c.fact_id)
           c.fact_id, c.asset_id, c.provider_mapping_id, c.period_start, c.period_end,
           c.value, p.is_fallback, p.priority
    FROM current_revision c
    LEFT JOIN asset_source_policies p
           ON p.asset_id = c.asset_id
          AND p.source_use = 'production'
          AND p.provider_mapping_id = c.provider_mapping_id
          AND p.valid_from <= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start))
          AND (p.valid_to IS NULL
               OR p.valid_to >= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start)))
    ORDER BY c.fact_id, p.is_fallback ASC NULLS LAST, p.priority ASC NULLS LAST, p.id ASC NULLS LAST
),
canonical AS (
    SELECT DISTINCT ON (s.asset_id, s.period_start, s.period_end) s.*
    FROM scored s
    ORDER BY s.asset_id, s.period_start, s.period_end,
             s.is_fallback ASC NULLS LAST, s.priority ASC NULLS LAST, s.provider_mapping_id ASC
)
SELECT c.asset_id,
       date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start)) AS source_day,
       count(*) AS sources,
       sum(c.value) AS summed_before,
       max(k.value) AS kept_after
FROM current_revision c
LEFT JOIN canonical k ON k.fact_id = c.fact_id
WHERE c.value IS NOT NULL
GROUP BY 1, 2, c.period_start, c.period_end
HAVING count(*) > 1
ORDER BY 1, 2;

\echo ''
\echo '== 4. assets whose facts have no production policy at all (kept, ranked last) =='
WITH current_revision AS (
    SELECT DISTINCT ON (f.provider_mapping_id, f.source_fact_key)
           f.id AS fact_id, f.asset_id, f.provider_mapping_id,
           f.period_start, f.period_end, f.value, f.metadata_json
    FROM production_facts f
    WHERE f.metric_kind = 'production_energy'
    ORDER BY f.provider_mapping_id, f.source_fact_key, f.source_revision DESC
),
-- The chosen source per fact, ranked exactly as monitoring.repository
-- .canonical_facts ranks it: policy primary, then policy fallback, then a
-- mapping with no policy for that day.
scored AS (
    SELECT DISTINCT ON (c.fact_id)
           c.fact_id, c.asset_id, c.provider_mapping_id, c.period_start, c.period_end,
           c.value, p.is_fallback, p.priority
    FROM current_revision c
    LEFT JOIN asset_source_policies p
           ON p.asset_id = c.asset_id
          AND p.source_use = 'production'
          AND p.provider_mapping_id = c.provider_mapping_id
          AND p.valid_from <= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start))
          AND (p.valid_to IS NULL
               OR p.valid_to >= date(timezone(COALESCE(c.metadata_json ->> 'source_timezone', 'UTC'), c.period_start)))
    ORDER BY c.fact_id, p.is_fallback ASC NULLS LAST, p.priority ASC NULLS LAST, p.id ASC NULLS LAST
),
canonical AS (
    SELECT DISTINCT ON (s.asset_id, s.period_start, s.period_end) s.*
    FROM scored s
    ORDER BY s.asset_id, s.period_start, s.period_end,
             s.is_fallback ASC NULLS LAST, s.priority ASC NULLS LAST, s.provider_mapping_id ASC
)
SELECT count(DISTINCT c.asset_id) AS assets_without_policy
FROM canonical c
WHERE c.is_fallback IS NULL;

ROLLBACK;
