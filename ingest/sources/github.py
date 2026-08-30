"""New and fast-growing repositories via the GitHub search API.

Catches tooling before it reaches the blogs. Authenticated requests get 30 searches per
minute against 10 unauthenticated, so GITHUB_TOKEN is used when present — GitHub Actions
injects it automatically and for free.

`published_at` is the repository's creation date, not its last push: the signal is "this
appeared recently and people starred it", not "someone committed today".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dlt
import httpx

import settings
from ingest.sources._common import ITEM_COLUMNS, build_item, utc_from_iso

SEARCH_URL = "https://api.github.com/search/repositories"
PER_PAGE = 50


def github_resource(src: dict):
    """Build a dlt resource for one GitHub search config."""
    source_name = src["name"]
    weight = src.get("weight", 1.0)
    topics = src.get("topics", ["llm"])
    min_stars = src.get("min_stars", 100)
    created_within_days = src.get("created_within_days", 7)

    @dlt.resource(
        name=f"github_{source_name}",
        table_name="items",
        write_disposition="append",
        primary_key="url_hash",
        columns=ITEM_COLUMNS,
    )
    def _github(
        published_at=dlt.sources.incremental(
            "published_at",
            initial_value=datetime.now(timezone.utc)
            - timedelta(days=created_within_days),
        ),
    ):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=created_within_days)
        ).date()

        headers = {"Accept": "application/vnd.github+json"}
        token = settings.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # One request per topic. The REST search API rejects `topic:a OR topic:b` with
        # a 422, and space-separating them ANDs the qualifiers instead — a repo would
        # have to carry every topic to match. Merging separate searches is the only
        # way to express "any of these topics".
        seen: set[int] = set()

        with httpx.Client(timeout=30, headers=headers) as client:
            for topic in topics:
                response = client.get(
                    SEARCH_URL,
                    params={
                        "q": f"topic:{topic} created:>{cutoff} stars:>{min_stars}",
                        "sort": "stars",
                        "order": "desc",
                        "per_page": PER_PAGE,
                    },
                )
                response.raise_for_status()

                for repo in response.json().get("items", []):
                    if repo["id"] in seen:
                        continue
                    seen.add(repo["id"])

                    yield build_item(
                        source_name=source_name,
                        source_type="github",
                        source_weight=weight,
                        external_id=repo["id"],
                        url=repo["html_url"],
                        title=repo["full_name"],
                        summary=repo.get("description"),
                        author=(repo.get("owner") or {}).get("login"),
                        published_at=utc_from_iso(repo["created_at"]),
                        engagement={
                            "stars": repo.get("stargazers_count"),
                            "forks": repo.get("forks_count"),
                        },
                    )

    return _github()
