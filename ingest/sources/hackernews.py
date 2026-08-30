"""Hacker News via the Algolia search API.

Free, no key, no rate-limit headaches at this volume, and it carries engagement signal
(points, comments) that pure RSS feeds do not.

HN items have two URLs: the story link and the HN discussion. We canonicalize on the
story link so an article that also appears on arXiv or a vendor blog deduplicates
against it, and keep the discussion link separately so the digest can offer both.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dlt
import httpx

from ingest.sources._common import ITEM_COLUMNS, build_item, utc_from_epoch

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
DISCUSSION_URL = "https://news.ycombinator.com/item?id={}"

LOOKBACK_DAYS = 3
HITS_PER_PAGE = 100


def hn_resource(src: dict):
    """Build a dlt resource for one Hacker News source config."""
    source_name = src["name"]
    weight = src.get("weight", 1.0)
    queries = src.get("queries", ["LLM"])
    min_points = src.get("min_points", 50)

    @dlt.resource(
        name=f"hn_{source_name}",
        table_name="items",
        write_disposition="append",
        primary_key="url_hash",
        columns=ITEM_COLUMNS,
    )
    def _hn(
        published_at=dlt.sources.incremental(
            "published_at",
            initial_value=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
        ),
    ):
        since = int(
            (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp()
        )
        # One story can match several queries; yielding it twice would inflate the
        # engagement window functions downstream, so dedupe within the run.
        seen: set[str] = set()

        with httpx.Client(timeout=30) as client:
            for query in queries:
                response = client.get(
                    SEARCH_URL,
                    params={
                        "tags": "story",
                        "query": query,
                        "numericFilters": f"created_at_i>{since},points>{min_points}",
                        "hitsPerPage": HITS_PER_PAGE,
                    },
                )
                response.raise_for_status()

                for hit in response.json().get("hits", []):
                    object_id = hit.get("objectID")
                    if not object_id or object_id in seen:
                        continue
                    seen.add(object_id)

                    discussion = DISCUSSION_URL.format(object_id)
                    # Ask HN / Show HN text posts have no external URL; the discussion
                    # itself is the item.
                    story_url = hit.get("url") or discussion

                    yield build_item(
                        source_name=source_name,
                        source_type="hackernews",
                        source_weight=weight,
                        external_id=object_id,
                        url=story_url,
                        title=hit.get("title") or hit.get("story_title") or "",
                        summary=hit.get("story_text") or hit.get("comment_text"),
                        author=hit.get("author"),
                        published_at=utc_from_epoch(hit["created_at_i"]),
                        discussion_url=discussion,
                        engagement={
                            "points": hit.get("points"),
                            "comments": hit.get("num_comments"),
                        },
                    )

    return _hn()
