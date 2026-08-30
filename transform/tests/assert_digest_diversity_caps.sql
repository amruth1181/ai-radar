-- The digest must never be dominated by one source or one category.
--
-- Guards the failure that made this necessary: a real run returned 9 of 12 items about
-- a single model release, all from one subreddit. Individually well-scored,
-- collectively useless.
--
-- A singular test rather than a unit test on purpose. fct_daily_digest filters on a
-- 26-hour window relative to now, so fixture timestamps would go stale; this asserts
-- the invariant against real output on every build instead.

with source_counts as (

    select source_name as offender, 'source' as dimension, count(*) as n
    from {{ ref('fct_daily_digest') }}
    group by source_name
    having count(*) > {{ var('digest_max_per_source', 3) }}

),

category_counts as (

    select category as offender, 'category' as dimension, count(*) as n
    from {{ ref('fct_daily_digest') }}
    group by category
    having count(*) > {{ var('digest_max_per_category', 4) }}

)

select * from source_counts
union all
select * from category_counts
