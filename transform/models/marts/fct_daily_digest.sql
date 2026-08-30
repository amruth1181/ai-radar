{{ config(materialized='table') }}

-- Today's digest: the handful of items actually worth reading.
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

-- Diversity caps.
--
-- Pure top-N by score does not produce a digest, it produces whatever the loudest
-- corner of the internet did yesterday. A real run returned 9 of 12 items about one
-- model release, all from one subreddit -- individually well-scored, collectively
-- useless, because reading item 9 taught you nothing item 1 had not.
--
-- Capping per source is what actually fixes that case: near-duplicate posts about one
-- hot topic overwhelmingly arrive through a single source. Capping per category
-- handles the other shape, a busy arXiv day burying every tooling item.
--
-- Entity-based topic clustering was considered and rejected: the LLM emits "Qwen",
-- "Qwen3.8-Next" and "Qwen-3.8 27B" for the same subject, so exact matching does not
-- group them and fuzzy matching is not worth the fragility.
-- The caps are applied in sequence, not together. Ranking both at once lets an item
-- the source cap is about to discard still consume a category slot: on a real run a
-- fourth Reddit tooling post held the last tooling slot and pushed out the only
-- GitHub item, which then failed the category cap despite being alone in its source.
source_capped as (

    select *
    from (
        select
            *,
            row_number() over (
                partition by source_name order by final_score desc, url_hash
            ) as rank_in_source
        from eligible
    ) ranked_by_source
    where rank_in_source <= {{ var('digest_max_per_source', 3) }}

),

diverse as (

    select *
    from (
        select
            *,
            row_number() over (
                partition by category order by final_score desc, url_hash
            ) as rank_in_category
        from source_capped
    ) ranked_by_category
    where rank_in_category <= {{ var('digest_max_per_category', 4) }}

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
    relevance_score,
    final_score,
    published_at,
    age_hours

from diverse

-- Deliberately no backfill when the caps leave fewer than digest_size items. A short,
-- varied digest is more useful than a long, repetitive one, and the quiet-day line in
-- the message already explains a small count.
qualify row_number() over (order by final_score desc) <= {{ var('digest_size', 12) }}
