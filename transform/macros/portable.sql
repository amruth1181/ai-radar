{#
    Cross-dialect helpers.

    DuckDB is the dev warehouse and BigQuery is the prod target, and the two disagree
    on JSON access and timestamp literals. Isolating those differences here keeps every
    model readable and means Phase 4 does not require rewriting the SQL.
#}

{% macro json_get(column, path) %}
    {%- if target.type == 'duckdb' -%}
        json_extract_string({{ column }}, '$.{{ path }}')
    {%- else -%}
        json_value({{ column }}, '$.{{ path }}')
    {%- endif -%}
{% endmacro %}


{% macro epoch_start() %}
    {%- if target.type == 'duckdb' -%}
        cast('1970-01-01 00:00:00+00' as timestamptz)
    {%- else -%}
        timestamp('1970-01-01 00:00:00+00')
    {%- endif -%}
{% endmacro %}


{% macro hours_ago(n) %}
    {%- if target.type == 'duckdb' -%}
        (now() - interval {{ n }} hour)
    {%- else -%}
        timestamp_sub(current_timestamp(), interval {{ n }} hour)
    {%- endif -%}
{% endmacro %}



{% macro json_array_first(column) %}
    {#- First element of a JSON array. The path syntax is the same in both engines;
        only the function name differs. -#}
    {%- if target.type == 'duckdb' -%}
        json_extract_string({{ column }}, '$[0]')
    {%- else -%}
        json_value({{ column }}, '$[0]')
    {%- endif -%}
{% endmacro %}


{% macro topic_key(entities_column, fallback) %}
    {#- A coarse subject key for grouping near-duplicate items.

        The enrichment prompt lists the primary subject first, so element 0 is the
        item's topic. Stripping to its leading alphabetic run collapses the version
        noise that made exact matching useless: "Qwen", "Qwen3.8-27B" and
        "Qwen3.8-Flash-Next" all become "qwen", while "Tencent" and "Vuk97" stay
        distinct.

        Falls back to url_hash when there are no entities, so unclassified items each
        form their own group and are never capped against one another.

        No r'' prefix on the pattern: that is BigQuery raw-string syntax and DuckDB
        reads the r as a type name. The pattern has no backslashes, so plain quotes
        work in both. -#}
    coalesce(
        nullif(lower(regexp_extract({{ json_array_first(entities_column) }}, '^[A-Za-z]+')), ''),
        {{ fallback }}
    )
{% endmacro %}
