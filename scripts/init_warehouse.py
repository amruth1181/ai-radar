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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

import settings  # noqa: E402

DEFAULT_DB = settings.REPO_ROOT / "ai_radar.duckdb"

# Enrichment lives apart from raw.items on purpose: the prompt can be rewritten and
# re-run without touching ingested data, and two prompt versions can be diffed.
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


def init(db_path: Path | str = DEFAULT_DB) -> None:
    con = duckdb.connect(str(db_path))
    try:
        for statement in DDL:
            con.execute(statement)
    finally:
        con.close()


def main() -> int:
    db_path = settings.get("DUCKDB_PATH") or DEFAULT_DB
    init(db_path)

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
