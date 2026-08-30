{{ config(materialized='view') }}

-- Light cleaning only. No deduplication here: staging's job is to make raw rows
-- trustworthy, and int_items_dedup's job is to collapse them.

select
    url_hash,
    source_name,
    source_type,
    source_weight,
    url,
    discussion_url,
    trim(title)     as title,
    author,
    summary_raw,
    published_at,
    fetched_at,
    engagement

from {{ source('raw', 'items') }}

where title is not null
  -- A title under ~10 characters is a parse artefact, not an article.
  and length(trim(title)) > 10
  and url_hash is not null
  and published_at is not null
  -- Some feeds publish into the future, either as a scheduling quirk or a timezone
  -- bug upstream. Those items would dominate a recency-weighted ranking forever.
  and published_at <= {{ dbt.current_timestamp() }}
  -- Guards epoch-0: a failed date parse becomes 1970 and silently ranks as ancient.
  and extract(year from published_at) >= 2020
