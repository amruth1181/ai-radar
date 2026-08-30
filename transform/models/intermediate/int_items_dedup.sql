{{ config(materialized='incremental', unique_key='url_hash') }}

-- The core of the project: one row per canonical URL.
--
-- A paper can appear on arXiv, then Hacker News, then Reddit. We want the arXiv
-- timestamp (earliest sighting), the HN points (maximum engagement), and the fact
-- that three independent sources surfaced it (corroboration).

with source_rows as (

    select * from {{ ref('stg_items') }}

),

{% if is_incremental() %}

-- Incremental on fetched_at, not published_at.
--
-- Filtering on published_at would miss late corroboration: a paper published two days
-- ago on arXiv that reaches Hacker News today still has a two-day-old published_at, so
-- it would fall outside the window and never pick up the HN points or the higher
-- source_count. fetched_at moves whenever any source sees the item again.
touched as (

    select distinct url_hash
    from source_rows
    where fetched_at > (
        select coalesce(max(last_fetched_at), {{ epoch_start() }}) from {{ this }}
    )

),

-- Re-aggregate every sighting of a touched hash, not just the new ones, or the
-- rebuilt row would lose the sources it was already seen in.
base as (

    select * from source_rows
    where url_hash in (select url_hash from touched)

),

{% else %}

base as (

    select * from source_rows

),

{% endif %}

signals as (

    select
        url_hash,
        count(*)                as source_count,
        min(published_at)       as published_at,
        max(fetched_at)         as last_fetched_at,
        -- Ordered so the value is deterministic: without it the aggregate's order
        -- depends on scan order, which makes the column untestable and produces
        -- spurious diffs between runs.
        string_agg(distinct source_name, ',' order by source_name) as seen_in,

        max(cast({{ json_get('engagement', 'points') }} as bigint))   as max_points,
        max(cast({{ json_get('engagement', 'comments') }} as bigint)) as max_comments,
        max(cast({{ json_get('engagement', 'stars') }} as bigint))    as max_stars

    from base
    group by url_hash

),

-- Which sighting's metadata to keep: prefer the most trusted source, and among equals
-- the earliest. A vendor blog's title and summary beat an aggregator's rehosting.
primary_sighting as (

    select
        url_hash,
        source_name,
        source_type,
        source_weight,
        url,
        discussion_url,
        title,
        author,
        summary_raw
    from base
    qualify row_number() over (
        partition by url_hash
        order by source_weight desc, published_at asc, source_name asc
    ) = 1

)

select
    p.url_hash,
    p.source_name,
    p.source_type,
    p.source_weight,
    p.url,
    p.discussion_url,
    p.title,
    p.author,
    p.summary_raw,

    s.published_at,
    s.last_fetched_at,
    s.source_count,
    s.seen_in,
    s.max_points,
    s.max_comments,
    s.max_stars,

    -- An item independently surfaced by three sources is probably important. Capped,
    -- because the fourth and fifth sightings add far less evidence than the second.
    least(s.source_count, 3) as corroboration

from primary_sighting p
join signals s on p.url_hash = s.url_hash
