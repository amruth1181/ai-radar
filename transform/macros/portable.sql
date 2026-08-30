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

