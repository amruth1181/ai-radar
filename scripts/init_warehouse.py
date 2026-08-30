"""Create the warehouse tables that neither dlt nor dbt owns.

Three writers touch this warehouse:

    dlt   -> raw.items          (append-only ingestion)
    dbt   -> analytics.*        (transformations)
    Python -> raw.enrichments   (LLM triage output)
              raw.sent_items    (delivery ledger)

The last two are written by plain Python, so nothing else creates them. dbt reads
raw.enrichments in fct_items, which means it has to exist — empty is fine — before the
first dbt build. Idempotent: safe to run on every pipeline execution.

    uv run python scripts/init_warehouse.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

import settings  # noqa: E402

DEFAULT_DB = settings.REPO_ROOT / "ai_radar.duckdb"

# Enrichment lives apart from raw.items on purpose: the prompt can be rewritten and
# re-run without touching ingested data, and two prompt versions can be diffed.
# dlt owns raw.items and creates it on first load. CI never ingests, so it needs an
# empty one to build against -- hence the opt-in flag rather than creating it always,
# which would risk dlt loading into a table it did not define.
ITEMS_DDL = """
create table if not exists raw.items (
    url_hash        varchar not null,
    source_name     varchar,
    source_type     varchar,
    source_weight   double,
    external_id     varchar,
    url             varchar,
    discussion_url  varchar,
    title           varchar,
    author          varchar,
    summary_raw     varchar,
    published_at    timestamptz,
    fetched_at      timestamptz,
    engagement      json,
    _dlt_load_id    varchar,
    _dlt_id         varchar
)
"""

DDL = [
    "create schema if not exists raw",
    """
    create table if not exists raw.enrichments (
        url_hash        varchar not null,
        summary         varchar,
        category        varchar,
        entities        json,
        relevance_score bigint,
        reason          varchar,
        model           varchar,
        enriched_at     timestamptz
    )
    """,
    # Without this ledger the 26-hour digest window resends yesterday's top item,
    # which is the fastest way to stop trusting the digest.
    """
    create table if not exists raw.sent_items (
        url_hash varchar not null,
        channel  varchar not null,
        sent_at  timestamptz not null
    )
    """,
]


def init(db_path: Path | str = DEFAULT_DB, with_items: bool = False) -> None:
    con = duckdb.connect(str(db_path))
    try:
        for statement in DDL:
            con.execute(statement)
        if with_items:
            con.execute(ITEMS_DDL)
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Python-owned warehouse tables.")
    parser.add_argument(
        "--with-items",
        action="store_true",
        help="also create an empty raw.items (CI only; dlt owns it otherwise)",
    )
    args = parser.parse_args()

    db_path = settings.get("DUCKDB_PATH") or DEFAULT_DB
    init(db_path, with_items=args.with_items)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for table in ("raw.items", "raw.enrichments", "raw.sent_items"):
            try:
                count = con.execute(f"select count(*) from {table}").fetchone()[0]
                print(f"  {table:<20} {count:>6} rows")
            except duckdb.CatalogException:
                print(f"  {table:<20} {'absent':>6}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
