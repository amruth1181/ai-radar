"""dlt pipeline entrypoint.

Fail-soft is the central design rule here: one dead feed must never cost you the
digest. Each source runs in its own load; failures are collected and reported so they
surface in the message footer rather than aborting the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import dlt
import yaml

from warehouse import BIGQUERY_SCHEMAS, DUCKDB_SCHEMAS
from ingest.sources.github import github_resource
from ingest.sources.hackernews import hn_resource
from ingest.sources.rss import rss_resource

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

PIPELINE_NAME = "ai_radar"


def pipeline_name_for(destination: str) -> str:
    """Per-destination pipeline name, so dev and prod keep separate cursors.

    dlt stores incremental state both in the destination and locally under
    ~/.dlt/pipelines/<pipeline_name>. Sharing one name across targets means a local
    dev run's cursor is reused for prod: dlt concludes it has already fetched
    everything and loads a single row into an empty BigQuery.

    A fresh CI runner has no local state and would not hit this, which is exactly why
    it is worth fixing -- it only misleads during local testing, when you are deciding
    whether prod works.
    """
    return f"{PIPELINE_NAME}_{destination}"


def dataset_for(destination: str) -> str:
    """Where dlt writes the raw layer.

    DuckDB has one file with a `raw` schema; BigQuery has separate datasets on a
    shared project, where a dataset called plain `raw` would collide with other work.
    Hardcoding "raw" made dlt create exactly that dataset on BigQuery while dbt looked
    in ai_radar_raw, so ingestion succeeded and every model failed with "table not
    found". Both sides now read the same mapping.
    """
    schemas = BIGQUERY_SCHEMAS if destination == "bigquery" else DUCKDB_SCHEMAS
    return schemas["raw"]

# Source type -> resource factory. Each factory takes the whole config entry, because
# the types need different keys: an RSS feed has a url, HN has queries and a points
# floor, GitHub has topics. Reddit is an RSS feed now -- its JSON API requires
# manual approval that hobby projects rarely get.
DISPATCH = {
    "rss": rss_resource,
    "hackernews": hn_resource,
    "github": github_resource,
}


@dataclass
class RunReport:
    """What happened, in a shape the digest footer can render."""

    loaded: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.loaded.values())

    def summary(self) -> str:
        parts = [f"{self.total_rows} rows from {len(self.loaded)} sources"]
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return " · ".join(parts)


def load_sources(path: Path = CONFIG_PATH) -> list[dict]:
    with open(path) as fh:
        return yaml.safe_load(fh)["sources"]


def run(destination: str = "duckdb", only: str | None = None) -> RunReport:
    """Ingest every configured source. `only` restricts to a single source name."""
    pipeline = dlt.pipeline(
        pipeline_name=pipeline_name_for(destination),
        destination=destination,
        dataset_name=dataset_for(destination),
    )

    report = RunReport()

    for src in load_sources():
        name = src["name"]
        if only and name != only:
            continue

        factory = DISPATCH.get(src["type"])
        if factory is None:
            report.skipped.append((name, f"unsupported type '{src['type']}'"))
            continue

        try:
            pipeline.run(factory(src))
            report.loaded[name] = _rows_loaded(pipeline)
        except Exception as exc:  # noqa: BLE001 - fail soft, report at the end
            reason = _root_cause(exc)
            log.warning("source %s failed: %s", name, reason)
            report.failures.append((name, reason))

    return report


def _root_cause(exc: BaseException) -> str:
    """Unwrap to the innermost exception message.

    dlt wraps extraction errors in PipelineStepFailed -> ResourceExtractionError, so
    str(exc) is several lines of load-package plumbing with the actual reason buried at
    the bottom. This text ends up in the digest footer, where only the reason matters.
    """
    seen = set()
    current = exc
    while True:
        nested = current.__cause__ or current.__context__
        if nested is None or id(nested) in seen:
            break
        seen.add(id(nested))
        current = nested

    message = " ".join(str(current).split())[:200]
    # Some exceptions stringify to nothing at all -- KeyboardInterrupt is the common
    # one. An empty footer entry reads as "this source failed for no reason", so fall
    # back to the class name.
    return message or type(current).__name__


def _rows_loaded(pipeline) -> int:
    """Rows written to `items` by the most recent load.

    The normalize trace carries real row counts; load-package jobs are per-file and
    would only tell us how many files were written.
    """
    try:
        return pipeline.last_trace.last_normalize_info.row_counts.get("items", 0)
    except AttributeError:
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run()
    print(result.summary())
    for source, error in result.failures:
        print(f"  FAILED  {source}: {error[:120]}")
    for source, reason in result.skipped:
        print(f"  skipped {source}: {reason}")
