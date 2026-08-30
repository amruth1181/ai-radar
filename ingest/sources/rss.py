"""Generic RSS/Atom resource.

One resource *type* serves every feed. Adding a feed is one line in sources.yaml, not
new code.

Each feed gets its own dlt resource name (and therefore its own incremental cursor)
while writing into the shared `items` table. This matters: if all 14 feeds shared one
cursor, a high-volume feed like arXiv would push it forward and starve a low-volume
feed like Simon Willison's, which publishes a few times a week.
"""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timedelta, timezone

import dlt
import feedparser

from ingest.normalize import canonicalize, strip_html, url_hash

# Feeds backfill and correct themselves; start behind the present so a first run picks
# up a useful window rather than only what appeared in the last few minutes.
LOOKBACK_DAYS = 3

SUMMARY_MAX_CHARS = 4000


def _parse_date(entry) -> datetime | None:
    """Return a timezone-aware UTC datetime, or None if the entry has no usable date.

    feedparser normalizes `*_parsed` struct_times to UTC, so `timegm` (not `mktime`,
    which assumes local time) is the correct inverse. Returning a naive datetime here
    would silently corrupt the dlt incremental cursor — this bites everyone once.
    """
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(field)
        if struct:
            return datetime.fromtimestamp(timegm(struct), tz=timezone.utc)
    return None


def rss_resource(source_name: str, feed_url: str, weight: float, **_ignored):
    """Build a dlt resource for one feed."""

    @dlt.resource(
        name=f"rss_{source_name}",
        table_name="items",
        write_disposition="append",
        primary_key="url_hash",
        # RSS never populates these, so dlt cannot infer their types and would leave
        # them unmaterialized. Declaring them keeps one stable `items` schema that dbt
        # can rely on before the HN/Reddit sources start filling them in Phase 1.
        columns={
            "discussion_url": {"data_type": "text", "nullable": True},
            "engagement": {"data_type": "json", "nullable": True},
            "author": {"data_type": "text", "nullable": True},
        },
    )
    def _feed(
        published_at=dlt.sources.incremental(
            "published_at",
            initial_value=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
        ),
    ):
        feed = feedparser.parse(feed_url)

        # A valid feed with zero entries is NOT an error. arXiv declares
        # <skipDays>Saturday,Sunday</skipDays> and serves an empty channel all weekend;
        # raising here would mark the source failed two days out of seven. The real
        # failure is a document we could not parse into a feed at all.
        #
        # `bozo` alone is also not fatal — plenty of live feeds have minor XML defects
        # yet still yield usable entries.
        if not feed.entries and not feed.feed.get("title"):
            raise RuntimeError(
                f"{source_name}: could not parse a feed from {feed_url} "
                f"({getattr(feed, 'bozo_exception', 'no exception reported')})"
            )

        for entry in feed.entries:
            published = _parse_date(entry)
            raw_url = entry.get("link")
            if published is None or not raw_url:
                continue

            yield {
                "source_name": source_name,
                "source_type": "rss",
                "source_weight": weight,
                "external_id": entry.get("id") or raw_url,
                "url": canonicalize(raw_url),
                "url_hash": url_hash(raw_url),
                "discussion_url": None,
                "title": (entry.get("title") or "").strip(),
                "author": entry.get("author"),
                "summary_raw": strip_html(entry.get("summary"))[:SUMMARY_MAX_CHARS],
                "published_at": published,
                "fetched_at": datetime.now(timezone.utc),
                "engagement": None,
            }

    return _feed()
