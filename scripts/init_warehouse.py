"""Create the warehouse tables that neither dlt nor dbt owns.

Three writers touch this warehouse:

    dlt    -> raw.items          append-only ingestion
    dbt    -> analytics.*        transformations
    Python -> raw.enrichments    LLM triage output
              raw.sent_items     delivery ledger

Nothing else creates that third group, and dbt reads raw.enrichments in fct_items, so
it has to exist -- empty is fine -- before the first build.

Runs against whichever target is configured, because the tables are needed in prod
exactly as much as in dev. The column types differ between DuckDB and BigQuery, which
is the only reason this is not a single DDL string.

    uv run python scripts/init_warehouse.py
    uv run python scripts/init_warehouse.py --with-items   # CI only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warehouse import BIGQUERY_SCHEMAS, DUCKDB_SCHEMAS, Warehouse, get_warehouse  # noqa: E402

# Logical type -> physical type, per dialect.
TYPES = {
    "duckdb": {
        "text": "varchar",
        "int": "bigint",
        "float": "double",
        "timestamp": "timestamptz",
        "json": "json",
    },
    "bigquery": {
        "text": "string",
        "int": "int64",
        "float": "float64",
        "timestamp": "timestamp",
        "json": "json",
    },
}

# Enrichment is kept apart from raw.items on purpose: the prompt can be rewritten and
# replayed without touching ingested data, and two prompt versions can be diffed.
ENRICHMENTS = [
    ("url_hash", "text"),
    ("summary", "text"),
    ("category", "text"),
    ("entities", "json"),
    ("relevance_score", "int"),
    ("reason", "text"),
    ("model", "text"),
    ("enriched_at", "timestamp"),
]

# Without this ledger the 26-hour window resends yesterday's top item.
SENT_ITEMS = [
    ("url_hash", "text"),
    ("channel", "text"),
    ("sent_at", "timestamp"),
]

# dlt owns raw.items and creates it on first load. CI never ingests, so it needs an
# empty one to build against -- hence the opt-in flag rather than always creating it,
# which would risk dlt loading into a table it did not define.
ITEMS = [
    ("url_hash", "text"),
    ("source_name", "text"),
    ("source_type", "text"),
    ("source_weight", "float"),
    ("external_id", "text"),
    ("url", "text"),
    ("discussion_url", "text"),
    ("title", "text"),
    ("author", "text"),
    ("summary_raw", "text"),
    ("published_at", "timestamp"),
    ("fetched_at", "timestamp"),
    ("engagement", "json"),
    ("_dlt_load_id", "text"),
    ("_dlt_id", "text"),
]


def _dialect(wh: Warehouse) -> str:
    return "duckdb" if wh.target == "dev" else "bigquery"


def _create(wh: Warehouse, name: str, columns: list[tuple[str, str]]) -> None:
    types = TYPES[_dialect(wh)]
    body = ", ".join(f"{col} {types[kind]}" for col, kind in columns)
    wh.execute(f"create table if not exists {wh.table('raw', name)} ({body})")


def init(wh: Warehouse | None = None, with_items: bool = False) -> None:
    owned = wh is None
    wh = wh or get_warehouse()
    try:
        schemas = DUCKDB_SCHEMAS if _dialect(wh) == "duckdb" else BIGQUERY_SCHEMAS
        wh.execute(f"create schema if not exists {schemas['raw']}")

        _create(wh, "enrichments", ENRICHMENTS)
        _create(wh, "sent_items", SENT_ITEMS)
        if with_items:
            _create(wh, "items", ITEMS)
    finally:
        if owned:
            wh.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Python-owned warehouse tables.")
    parser.add_argument(
        "--with-items",
        action="store_true",
        help="also create an empty raw.items (CI only; dlt owns it otherwise)",
    )
    args = parser.parse_args()

    with get_warehouse() as wh:
        init(wh, with_items=args.with_items)
        print(f"target: {wh.target}")
        for table in ("items", "enrichments", "sent_items"):
            ref = wh.table("raw", table)
            try:
                count = wh.query(f"select count(*) as n from {ref}")[0]["n"]
                print(f"  {ref:<50} {count:>6} rows")
            except Exception:  # noqa: BLE001 - absent is a valid state to report
                print(f"  {ref:<50} {'absent':>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
