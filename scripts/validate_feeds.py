"""Check every configured source without loading anything.

Feed URLs rot — vendors move or retire them with no notice, and a dead source looks
exactly like a quiet week in the digest. Run this weekly, and before any deploy.

    uv run python scripts/validate_feeds.py

Statuses:
    OK      returned items
    EMPTY   reachable and parseable, but nothing in the window. Normal for arXiv at
            weekends (it declares <skipDays>Saturday,Sunday</skipDays>), suspicious
            for anything else more than a couple of days running.
    RATE    rate limited by the host; retry later
    AUTH    credentials missing or rejected
    DEAD    unreachable, or the response could not be parsed
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402
import httpx  # noqa: E402

import settings  # noqa: E402,F401  -- loads .env
from ingest.pipeline import load_sources  # noqa: E402
from ingest.sources.rss import USER_AGENT  # noqa: E402

LOOKBACK_DAYS = 3


def check_rss(src: dict) -> tuple[str, int, str]:
    # Same agent the resource uses. feedparser's default identifies as feedparser,
    # which Reddit throttles hard.
    feed = feedparser.parse(src["url"], agent=USER_AGENT)

    if getattr(feed, "status", None) == 429:
        return "RATE", 0, "rate limited — retry later"

    count = len(feed.entries)
    if count:
        return "OK", count, ""
    # A parseable channel with no items is not the same as a broken feed.
    if feed.feed.get("title"):
        return "EMPTY", 0, "valid feed, no items in window"
    return "DEAD", 0, str(getattr(feed, "bozo_exception", "unparseable"))


def check_hackernews(src: dict) -> tuple[str, int, str]:
    since = int(
        (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp()
    )
    total = 0
    for query in src.get("queries", ["LLM"]):
        response = httpx.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": "story",
                "query": query,
                "numericFilters": f"created_at_i>{since},points>{src.get('min_points', 50)}",
                "hitsPerPage": 100,
            },
            timeout=30,
        )
        if response.status_code != 200:
            return "DEAD", 0, f"HTTP {response.status_code}"
        total += len(response.json().get("hits", []))
    return ("OK" if total else "EMPTY"), total, ""


def check_github(src: dict) -> tuple[str, int, str]:
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=src.get("created_within_days", 7))
    ).date()
    headers = {"Accept": "application/vnd.github+json"}
    token = settings.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    total = 0
    for topic in src.get("topics", ["llm"]):
        response = httpx.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params={
                "q": f"topic:{topic} created:>{cutoff} stars:>{src.get('min_stars', 100)}",
                "per_page": 50,
            },
            timeout=30,
        )
        if response.status_code == 403:
            return "AUTH", 0, "rate limited — set GITHUB_TOKEN"
        if response.status_code != 200:
            return "DEAD", 0, f"HTTP {response.status_code}"
        total += len(response.json().get("items", []))
    return ("OK" if total else "EMPTY"), total, ""


CHECKS = {
    "rss": check_rss,
    "hackernews": check_hackernews,
    "github": check_github,
}


def main() -> int:
    sources = load_sources()
    print(f"Checking {len(sources)} sources\n")
    print(f"{'STATUS':<7} {'ITEMS':>6}  {'NAME':<22} DETAIL")
    print("-" * 78)

    counts: dict[str, int] = {}
    for src in sources:
        check = CHECKS.get(src["type"])
        if check is None:
            status, count, detail = "DEAD", 0, f"no checker for type '{src['type']}'"
        else:
            try:
                status, count, detail = check(src)
            except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
                status, count, detail = "DEAD", 0, " ".join(str(exc).split())[:60]

        counts[status] = counts.get(status, 0) + 1
        print(f"{status:<7} {count:>6}  {src['name']:<22} {detail}")

    print("-" * 78)
    print(" · ".join(f"{status} {n}" for status, n in sorted(counts.items())))

    # Only a genuinely broken source is worth failing CI over. EMPTY is normal at
    # weekends and AUTH just means an optional credential is absent.
    return 1 if counts.get("DEAD") else 0


if __name__ == "__main__":
    raise SystemExit(main())
