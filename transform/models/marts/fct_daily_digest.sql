{{ config(materialized='table') }}

-- Today's digest: the items actually worth reading.
--
-- 26 hours rather than 24, so a late or skipped run does not drop items into a gap.
-- The sent_items ledger is what stops that overlap resending yesterday's top item.

with eligible as (

    select *
    from {{ ref('fct_items') }}
    where published_at >= {{ hours_ago(26) }}
      -- Unenriched items have no score and cannot be ranked, so they are not eligible.
      and relevance_score is not null
      and relevance_score >= {{ var('digest_min_score', 5) }}
      and url_hash not in (
          select url_hash from {{ source('raw', 'sent_items') }}
      )

),

-- Tier 0: the diverse core.
--
-- Pure top-N by score does not produce a digest, it produces whatever the loudest
-- corner of the internet did yesterday. A real run returned 9 of 12 items about one
-- model release, all from one subreddit.
--
-- Three caps, because the problem has three shapes:
--   source    one noisy feed dominating
--   topic     one hot subject dominating, even across sources
--   category  a busy arXiv day burying every tooling item
--
-- Applied in SEQUENCE, not ranked together. Ranking at once lets an item a later cap
-- will discard still consume an earlier cap's slot: on real data a fourth Reddit
-- tooling post held the last tooling slot and pushed out the only GitHub item.
-- QUALIFY rather than a ranked subquery: it filters on the window function without
-- projecting a rank column, which would then need removing. DuckDB spells that
-- removal EXCLUDE and BigQuery spells it EXCEPT, so avoiding it avoids the split.
source_capped as (

    select * from eligible
    qualify row_number() over (
        partition by source_name order by final_score desc, url_hash
    ) <= {{ var('digest_max_per_source', 3) }}

),

topic_capped as (

    select * from source_capped
    qualify row_number() over (
        partition by topic_key order by final_score desc, url_hash
    ) <= {{ var('digest_max_per_topic', 2) }}

),

core as (

    select * from topic_capped
    qualify row_number() over (
        partition by category order by final_score desc, url_hash
    ) <= {{ var('digest_max_per_category', 4) }}

),

-- Tier 1: backfill.
--
-- On a thin day -- a weekend with arXiv closed, say -- the tight caps can leave only
-- four items from a pool of seventeen. Rather than ship four, top up from what the
-- caps excluded, still under a looser topic ceiling so the backfill cannot become a
-- single-subject block. Diversity is a preference here, not an absolute: a reader who
-- wanted four items would not have asked for ten.
backfill as (

    select * from eligible
    where url_hash not in (select url_hash from core)
    qualify row_number() over (
        partition by topic_key order by final_score desc, url_hash
    ) <= {{ var('backfill_max_per_topic', 4) }}

),

combined as (

    select *, 0 as tier from core
    union all
    select *, 1 as tier from backfill

)

select
    url_hash,
    title,
    url,
    discussion_url,
    summary,
    category,
    entities,
    reason,
    source_name,
    seen_in,
    source_count,
    corroboration,
    max_points,
    max_stars,
    topic_key,
    relevance_score,
    final_score,
    published_at,
    age_hours,
    tier

from combined

-- Tier before score: every diverse item outranks every backfilled one, however well
-- the backfilled item scored.
qualify row_number() over (order by tier, final_score desc, url_hash)
        <= {{ var('digest_size', 10) }}
