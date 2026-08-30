{{ config(materialized='table') }}

-- Today's digest: the handful of items actually worth reading.
--
-- 26 hours rather than 24, so a late or skipped run does not drop items into a gap.
-- The sent_items ledger is what stops that overlap resending yesterday's top item.

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

from {{ ref('fct_items') }}

where published_at >= {{ hours_ago(26) }}
  -- Unenriched items have no score and cannot be ranked, so they are not eligible.
  and relevance_score is not null
  and relevance_score >= {{ var('digest_min_score', 5) }}
  and url_hash not in (
      select url_hash from {{ source('raw', 'sent_items') }}
  )

qualify row_number() over (order by final_score desc) <= {{ var('digest_size', 12) }}
