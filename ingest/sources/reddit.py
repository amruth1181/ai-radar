"""Reddit via OAuth.

The old trick of hitting `reddit.com/r/<sub>/new.json` with a descriptive User-Agent is
dead — it returns 403 regardless of headers. Reddit now requires an OAuth token even for
public, read-only listings.

Register a *script* app at reddit.com/prefs/apps to get a client ID and secret, then this
uses the client-credentials grant: no user login, no refresh token, no callback URL.

Missing credentials raise a clear error rather than returning nothing. The pipeline's
fail-soft loop turns that into a reported failure in the digest footer, which is what you
want — a silently absent source looks identical to a quiet week.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dlt
import httpx

import settings
from ingest.sources._common import ITEM_COLUMNS, build_item, utc_from_epoch

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
PERMALINK_BASE = "https://www.reddit.com"

LOOKBACK_DAYS = 3
DEFAULT_LIMIT = 100


def _access_token(user_agent: str) -> str:
    """Client-credentials grant. Tokens last ~24h; we just fetch one per run."""
    client_id = settings.get("REDDIT_CLIENT_ID")
    client_secret = settings.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET are not set — "
            "create a 'script' app at reddit.com/prefs/apps"
        )

    response = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"reddit auth failed ({response.status_code}): {response.text[:200]}")
    return response.json()["access_token"]


def reddit_resource(src: dict):
    """Build a dlt resource for one subreddit config."""
    source_name = src["name"]
    weight = src.get("weight", 1.0)
    subreddit = src["subreddit"]
    min_upvotes = src.get("min_upvotes", 100)

    @dlt.resource(
        name=f"reddit_{source_name}",
        table_name="items",
        write_disposition="append",
        primary_key="url_hash",
        columns=ITEM_COLUMNS,
    )
    def _reddit(
        published_at=dlt.sources.incremental(
            "published_at",
            initial_value=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
        ),
    ):
        # Reddit asks for a descriptive User-Agent and throttles generic ones harder.
        user_agent = settings.get("REDDIT_USER_AGENT", "ai-radar/0.1")
        token = _access_token(user_agent)

        response = httpx.get(
            f"{API_BASE}/r/{subreddit}/new",
            headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
            params={"limit": DEFAULT_LIMIT},
            timeout=30,
        )
        response.raise_for_status()

        for child in response.json()["data"]["children"]:
            post = child["data"]
            if (post.get("ups") or 0) < min_upvotes:
                continue

            discussion = f"{PERMALINK_BASE}{post['permalink']}"
            # Self posts have no external target; the thread is the item.
            target = discussion if post.get("is_self") else (post.get("url") or discussion)

            yield build_item(
                source_name=source_name,
                source_type="reddit",
                source_weight=weight,
                external_id=post["id"],
                url=target,
                title=post.get("title") or "",
                summary=post.get("selftext"),
                author=post.get("author"),
                published_at=utc_from_epoch(post["created_utc"]),
                discussion_url=discussion,
                engagement={
                    "upvotes": post.get("ups"),
                    "comments": post.get("num_comments"),
                },
            )

    return _reddit()
