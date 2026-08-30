{{ config(materialized='table') }}

-- Deduplicated items joined to their LLM triage, with the final ranking score.
--
-- Materialized as a table rather than incremental: final_score decays with age, so
-- every row's score changes on every run. An incremental model would freeze old scores
-- and the ranking would drift out of date.

with enriched as (

    select
        i.*,

        e.summary,
        e.category,
        e.entities,
        e.relevance_score,
        e.reason,
        e.model as enrichment_model,
        e.enriched_at

    from {{ ref('int_items_dedup') }} i
    left join {{ source('raw', 'enrichments') }} e
        on i.url_hash = e.url_hash

)

select
    *,

    -- Hours old, used by the decay term below.
    {{ dbt.datediff('published_at', dbt.current_timestamp(), 'hour') }} as age_hours,

    round(
        -- The LLM's judgement against the profile is the main signal. Unenriched
        -- items score 0 rather than null, so they sort last instead of vanishing.
        coalesce(relevance_score, 0)

        -- Trust prior: a lab's release blog outranks an aggregator.
        * source_weight

        -- Independent corroboration is evidence. +15% per extra source, capped at 3.
        * (1 + 0.15 * (corroboration - 1))

        -- Recency decay, ~26% lost per day. Too aggressive and a good paper posted at
        -- 6pm never surfaces; too gentle and last week crowds out today.
        * exp(
            -0.30
            * {{ dbt.datediff('published_at', dbt.current_timestamp(), 'hour') }}
            / 24.0
          )
    , 3) as final_score

from enriched
