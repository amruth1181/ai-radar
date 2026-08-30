"""Select unenriched items, score them, write the results back.

Enrichment runs AFTER deduplication, deliberately. The same paper reaching arXiv, Hacker
News and Reddit is one row by this point, so it is scored — and paid for — once.

Results land in raw.enrichments rather than as columns on raw.items, so the prompt can
be rewritten and re-run without touching ingested data.

    uv run python -m enrich.run --limit 5 --dry-run   # see what would be scored
    uv run python -m enrich.run --limit 5             # a real 5-item trial
    uv run python -m enrich.run                       # the full window
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import duckdb
import yaml

import settings
from enrich.backends import get_backend
from enrich.prompts import build_system

log = logging.getLogger(__name__)

PROFILE_PATH = settings.REPO_ROOT / "config" / "profile.yaml"
DEFAULT_DB = settings.REPO_ROOT / "ai_radar.duckdb"

# Matches the digest window in fct_daily_digest. Wider than 24h so a late or skipped
# run does not drop items into a gap.
WINDOW_HOURS = 26

# Hard ceiling on items scored per run. A runaway loop is the only way this project
# gets expensive, so the cap is in code rather than in a spend alert alone.
MAX_ITEMS = 80

MIN_TITLE_CHARS = 15

SELECT_CANDIDATES = """
select
    i.url_hash,
    i.title,
    i.source_name,
    i.summary_raw,
    cast(i.published_at as varchar) as published_at
from analytics.int_items_dedup i
left join raw.enrichments e on i.url_hash = e.url_hash
where e.url_hash is null
  and i.published_at >= now() - interval {window} hour
order by i.source_weight desc, i.published_at desc
limit {limit}
"""


def load_profile(path: Path = PROFILE_PATH) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def _mostly_ascii(text: str, threshold: float = 0.7) -> bool:
    """Crude language gate. Cheap, and every item skipped is an item not paid for."""
    if not text:
        return False
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars / len(text) >= threshold


def prefilter(items: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Drop items not worth spending a request on. Returns (kept, [(hash, reason)])."""
    kept, dropped = [], []
    for item in items:
        title = (item.get("title") or "").strip()
        if len(title) < MIN_TITLE_CHARS:
            dropped.append((item["url_hash"], "title too short"))
        elif not _mostly_ascii(title):
            dropped.append((item["url_hash"], "not English"))
        else:
            kept.append(item)
    return kept, dropped


def fetch_candidates(con, limit: int = MAX_ITEMS, window: int = WINDOW_HOURS) -> list[dict]:
    query = SELECT_CANDIDATES.format(window=window, limit=limit)
    cursor = con.execute(query)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def write_back(con, rows: list[dict]) -> int:
    if not rows:
        return 0
    con.executemany(
        """
        insert into raw.enrichments
            (url_hash, summary, category, entities, relevance_score, reason,
             model, enriched_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["url_hash"],
                r["summary"],
                r["category"],
                json.dumps(r["entities"]),
                r["relevance_score"],
                r["reason"],
                r["model"],
                r["enriched_at"],
            )
            for r in rows
        ],
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich unscored items.")
    parser.add_argument("--limit", type=int, default=MAX_ITEMS, help="max items to score")
    parser.add_argument("--window", type=int, default=WINDOW_HOURS, help="lookback hours")
    parser.add_argument("--backend", help="override ENRICH_BACKEND")
    parser.add_argument(
        "--dry-run", action="store_true", help="show candidates without calling the API"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db_path = settings.get("DUCKDB_PATH") or DEFAULT_DB
    con = duckdb.connect(str(db_path))
    try:
        candidates = fetch_candidates(con, limit=args.limit, window=args.window)
        kept, dropped = prefilter(candidates)

        print(f"{len(candidates)} candidates · {len(kept)} to score · {len(dropped)} filtered")
        for url_hash, reason in dropped:
            log.info("skipped %s: %s", url_hash, reason)

        if args.dry_run:
            for item in kept:
                print(f"  {item['source_name']:<20} {item['title'][:60]}")
            return 0

        if not kept:
            print("nothing to enrich")
            return 0

        backend = get_backend(args.backend)
        print(f"backend: {backend.name} ({backend.model})")

        result = backend.enrich(kept, build_system(load_profile()))
        written = write_back(con, result.rows)

        print(f"{result.summary()} · {written} written")
        # The failure rate is the number that decides whether a backend is good
        # enough. Surfaced on every run rather than buried in logs.
        if result.failure_rate > 0.10:
            log.warning(
                "malformed-JSON rate %.0f%% is high — consider ENRICH_BACKEND=claude",
                result.failure_rate * 100,
            )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
