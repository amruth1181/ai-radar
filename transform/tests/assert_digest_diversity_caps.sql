-- The digest must never be dominated by one source, subject or category.
--
-- Guards the failure that made the caps necessary: a real run returned 9 of 12 items
-- about a single model release, all from one subreddit. Individually well-scored,
-- collectively useless.
--
-- Two different invariants, because the digest is built in two tiers:
--
--   tier 0  the diverse core, which must respect all three tight caps
--   tier 1  backfill, which deliberately relaxes them so a thin day still fills the
--           digest -- but never without limit
--
-- A singular test rather than a unit test on purpose. fct_daily_digest filters on a
-- window relative to now, so fixture timestamps would go stale; this asserts the
-- invariant against real output on every build instead.

with core as (

    select * from {{ ref('fct_daily_digest') }} where tier = 0

),

core_violations as (

    select source_name as offender, 'tier0 source' as dimension, count(*) as n
    from core group by source_name
    having count(*) > {{ var('digest_max_per_source', 3) }}

    union all

    select topic_key, 'tier0 topic', count(*)
    from core group by topic_key
    having count(*) > {{ var('digest_max_per_topic', 2) }}

    union all

    select category, 'tier0 category', count(*)
    from core group by category
    having count(*) > {{ var('digest_max_per_category', 4) }}

),

-- Across both tiers a subject can appear at most core-cap + backfill-cap times.
-- Without this ceiling the backfill would happily rebuild the single-topic block the
-- core caps just dismantled.
overall_topic_violations as (

    select topic_key as offender, 'overall topic' as dimension, count(*) as n
    from {{ ref('fct_daily_digest') }}
    group by topic_key
    having count(*) > {{ var('digest_max_per_topic', 2) }}
                    + {{ var('backfill_max_per_topic', 2) }}

)

select * from core_violations
union all
select * from overall_topic_violations
