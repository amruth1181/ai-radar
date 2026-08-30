"""One interface over DuckDB (dev) and BigQuery (prod).

dbt handles the dialect difference for models via macros. Python needs the same thing
for the handful of places it touches the warehouse directly: reading enrichment
candidates, writing enrichments, reading the digest, and appending to the sent ledger.

Without this every one of those would need a `if target == 'bigquery'` branch, and the
GitHub Actions run would take a different code path from the one tested locally.

Schema naming differs deliberately. DuckDB has one file with `raw` and `analytics`
schemas; BigQuery has separate datasets on a project. Callers never spell either out --
they ask for `wh.table("raw", "items")` and get whatever is correct for the target.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import settings

DEFAULT_DB = settings.REPO_ROOT / "ai_radar.duckdb"

# Logical schema -> physical name, per target.
DUCKDB_SCHEMAS = {"raw": "raw", "analytics": "analytics"}
BIGQUERY_SCHEMAS = {"raw": "ai_radar_raw", "analytics": "ai_radar_marts"}


class Warehouse(ABC):
    """The four operations Python performs against the warehouse."""

    target: str

    @abstractmethod
    def table(self, schema: str, name: str) -> str:
        """Fully-qualified, quoted table reference for this target."""

    @abstractmethod
    def query(self, sql: str) -> list[dict]:
        """Run a read query and return rows as dicts."""

    @abstractmethod
    def execute(self, sql: str) -> None:
        """Run a statement with no result set."""

    @abstractmethod
    def insert(self, schema: str, name: str, rows: list[dict]) -> int:
        """Append rows. Column order is taken from the first row's keys."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DuckDBWarehouse(Warehouse):
    target = "dev"

    def __init__(self, path: str | None = None):
        import duckdb

        self._con = duckdb.connect(str(path or settings.get("DUCKDB_PATH") or DEFAULT_DB))

    def table(self, schema: str, name: str) -> str:
        return f"{DUCKDB_SCHEMAS[schema]}.{name}"

    def query(self, sql: str) -> list[dict]:
        cursor = self._con.execute(sql)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def execute(self, sql: str) -> None:
        self._con.execute(sql)

    def insert(self, schema: str, name: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        self._con.executemany(
            f"insert into {self.table(schema, name)} "
            f"({', '.join(columns)}) values ({placeholders})",
            [[_duckdb_value(r[c]) for c in columns] for r in rows],
        )
        return len(rows)

    def close(self) -> None:
        self._con.close()


class BigQueryWarehouse(Warehouse):
    target = "prod"

    def __init__(self, project: str | None = None):
        from google.cloud import bigquery

        self.project = project or settings.get("BIGQUERY_PROJECT")
        if not self.project:
            raise RuntimeError(
                "BIGQUERY_PROJECT is not set. Required when DBT_TARGET=prod."
            )
        self._client = bigquery.Client(project=self.project)

    def table(self, schema: str, name: str) -> str:
        return f"`{self.project}.{BIGQUERY_SCHEMAS[schema]}.{name}`"

    def query(self, sql: str) -> list[dict]:
        return [dict(row) for row in self._client.query(sql).result()]

    def execute(self, sql: str) -> None:
        self._client.query(sql).result()

    def insert(self, schema: str, name: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        table_id = f"{self.project}.{BIGQUERY_SCHEMAS[schema]}.{name}"
        errors = self._client.insert_rows_json(
            table_id, [_bigquery_row(r) for r in rows]
        )
        if errors:
            raise RuntimeError(f"bigquery insert failed: {errors[:3]}")
        return len(rows)


def _duckdb_value(value: Any) -> Any:
    """DuckDB takes a JSON string for a JSON column."""
    return json.dumps(value) if isinstance(value, (list, dict)) else value


def _bigquery_row(row: dict) -> dict:
    """BigQuery's JSON API needs ISO strings for timestamps and JSON-encoded structures."""
    out = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif isinstance(value, (list, dict)):
            out[key] = json.dumps(value)
        else:
            out[key] = value
    return out


def get_warehouse(target: str | None = None) -> Warehouse:
    """Pick a warehouse from DBT_TARGET, so Python and dbt always agree."""
    target = (target or settings.get("DBT_TARGET", "dev") or "dev").lower()
    if target == "prod":
        return BigQueryWarehouse()
    if target == "dev":
        return DuckDBWarehouse()
    raise ValueError(f"unknown DBT_TARGET '{target}' (expected 'dev' or 'prod')")
