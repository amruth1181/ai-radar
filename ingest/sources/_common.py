"""Shared shape for every source.

Every resource yields the same row shape into the `items` table, whatever the upstream
API looks like. Keeping that construction in one place is what stops the four sources
from drifting apart and breaking the dbt models downstream.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ingest.normalize import canonicalize, strip_html, url_hash

SUMMARY_MAX_CHARS = 4000

# Declared explicitly so dlt does not have to infer them. A source that never populates
# a column would otherwise leave it unmaterialized, and the schema would depend on which
# source happened to load first.
ITEM_COLUMNS = {
    "url_hash": {"data_type": "text", "nullable": False},
    "discussion_url": {"data_type": "text", "nullable": True},
    "engagement": {"data_type": "json", "nullable": True},
    "author": {"data_type": "text", "nullable": True},
}


def build_item(
    *,
    source_name: str,
    source_type: str,
    source_weight: float,
    external_id: str,
    url: str,
    title: str,
    published_at: datetime,
    summary: str | None = None,
    author: str | None = None,
    discussion_url: str | None = None,
    engagement: dict | None = None,
) -> dict:
    """Assemble one row. `url` is canonicalized and hashed here, never by the caller."""
    return {
        "source_name": source_name,
        "source_type": source_type,
        "source_weight": source_weight,
        "external_id": str(external_id),
        "url": canonicalize(url),
        "url_hash": url_hash(url),
        "discussion_url": discussion_url,
        "title": (title or "").strip(),
        "author": author,
        "summary_raw": strip_html(summary)[:SUMMARY_MAX_CHARS],
        "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc),
        "engagement": engagement,
    }


def utc_from_epoch(seconds: float) -> datetime:
    """Epoch seconds to tz-aware UTC. Naive datetimes silently break dlt's cursor."""
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc)


def utc_from_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, normalising a trailing 'Z' to +00:00."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
