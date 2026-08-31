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
import logging
import re
from collections import defaultdict
from pathlib import Path

import yaml

import settings
from enrich.backends import get_backend
from enrich.prompts import build_system
from warehouse import Warehouse, get_warehouse

log = logging.getLogger(__name__)

PROFILE_PATH = settings.REPO_ROOT / "config" / "profile.yaml"

# Matches the digest window in fct_daily_digest. Wider than 24h so a late or skipped
# run does not drop items into a gap.
WINDOW_HOURS = 26

# Hard ceiling on items scored per run. A runaway loop is the only way this project
# gets expensive, so the cap stays in code rather than relying on a spend alert.
# ~300 requests against GLM's ~1000/day free tier still leaves 3x headroom.
MAX_ITEMS = 300

# The budget is spent PER SOURCE, not globally.
#
# A global "best-weighted 80" collapses the moment a high-volume source wakes up: on
# the first weekday, arXiv cs.CL published 156 papers and — sitting at weight 1.0,
# above every aggregator — consumed the entire budget. cs.LG (210 papers), Hacker
# News, GitHub and Reddit were scored zero times, and because unscored items age out
# of the 26-hour window they were not deferred, they were lost.
#
# A per-source quota guarantees every source reaches the model, and caps any single
# one. 14 sources x 20 is 280, comfortably inside MAX_ITEMS.
PER_SOURCE_QUOTA = 20

# How many rows to pull per source before ranking them locally. Larger than the quota
# so the keyword pre-filter has something to choose between.
FETCH_PER_SOURCE = 80

MIN_TITLE_CHARS = 15

# Words too common to signal anything, stripped when deriving keywords from the
# profile so "data" and "systems" do not match every paper ever written.
_STOPWORDS = frozenset("""
and the for with from that this into your role roles more than what when will
using use used based their there they which while about over under how why
data systems system model models learning ai llm llms new run running
""".split())

SELECT_CANDIDATES = """
select
    i.url_hash,
    i.title,
    i.source_name,
    i.summary_raw,
    cast(i.published_at as {text_type}) as published_at
from {dedup} i
left join {enrichments} e on i.url_hash = e.url_hash
where e.url_hash is null
  and i.published_at >= {cutoff}
-- Per source, not globally: a single high-volume source must not be able to
-- consume the whole budget before another source is reached at all.
qualify row_number() over (
    partition by i.source_name order by i.published_at desc
) <= {per_source}
"""


def load_profile(path: Path = PROFILE_PATH) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def profile_keywords(profile_cfg: dict) -> set[str]:
    """Distinctive terms used to rank items within a source before spending requests.

    Never used to drop an item — a paper the profile does not name can still be worth
    reading; it just loses a tiebreak when a source has more candidates than quota.

    Read from the explicit `keywords:` list in profile.yaml rather than derived from
    the prose. Derivation was tried and produced "benchmark", "design", "evaluation",
    "inference" and "generation" — words in nearly every ML abstract — which ranked an
    RNA foundation model top of a data-engineering feed. Curation beats extraction
    when the whole job is discrimination.

    Phrases are matched as substrings, so "vector database" and "kv cache" work.
    """
    return {
        str(term).lower().strip()
        for term in profile_cfg.get("keywords", [])
        if str(term).strip()
    }


def keyword_affinity(item: dict, keywords: set[str]) -> int:
    """How many profile terms this item's title and summary mention."""
    if not keywords:
        return 0
    text = f"{item.get('title') or ''} {item.get('summary_raw') or ''}".lower()
    return sum(1 for word in keywords if word in text)


def apply_quota(
    items: list[dict],
    keywords: set[str],
    quota: int = PER_SOURCE_QUOTA,
    limit: int = MAX_ITEMS,
) -> list[dict]:
    """Take the best `quota` items from each source, then cap the total.

    Ranking inside a source is by profile keyword matches, then recency. That is what
    turns 156 undifferentiated arXiv papers into the 20 most likely to matter, without
    discarding anything on a keyword rule alone.
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_source[item["source_name"]].append(item)

    kept: list[dict] = []
    for source_items in by_source.values():
        source_items.sort(
            key=lambda i: (keyword_affinity(i, keywords), i.get("published_at") or ""),
            reverse=True,
        )
        kept.extend(source_items[:quota])

    # Newest first once quotas are settled, so a truncated run still scores today.
    kept.sort(key=lambda i: i.get("published_at") or "", reverse=True)
    return kept[:limit]


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


def fetch_candidates(
    wh: Warehouse, window: int = WINDOW_HOURS, per_source: int = FETCH_PER_SOURCE
) -> list[dict]:
    """Unenriched items inside the window, up to `per_source` from each source."""
    return wh.query(
        SELECT_CANDIDATES.format(
            dedup=wh.table("analytics", "int_items_dedup"),
            enrichments=wh.table("raw", "enrichments"),
            cutoff=wh.hours_ago(window),
            text_type=wh.text_type,
            per_source=per_source,
        )
    )


def write_back(wh: Warehouse, rows: list[dict]) -> int:
    return wh.insert("raw", "enrichments", rows)


def enrich_pending(
    wh: Warehouse, limit: int = MAX_ITEMS, window: int = WINDOW_HOURS,
    backend_name: str | None = None,
):
    """Select, filter, score and persist. Returns the backend's EnrichmentResult."""
    from enrich.backends.base import EnrichmentResult

    profile = load_profile()
    candidates = fetch_candidates(wh, window=window)
    kept, dropped = prefilter(candidates)
    for url_hash, reason in dropped:
        log.info("skipped %s: %s", url_hash, reason)

    # Quota AFTER the cheap filters, so a source's allocation is not spent on rows
    # that were going to be discarded anyway.
    kept = apply_quota(kept, profile_keywords(profile), limit=limit)

    if not kept:
        return EnrichmentResult(attempted=0)

    backend = get_backend(backend_name)
    log.info("backend: %s (%s)", backend.name, backend.model)
    result = backend.enrich(kept, build_system(profile))
    write_back(wh, result.rows)

    if result.malformed_rate > 0.10:
        log.warning(
            "malformed-JSON rate %.0f%% is high — consider ENRICH_BACKEND=claude",
            result.malformed_rate * 100,
        )
    return result


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

    with get_warehouse() as wh:
        if args.dry_run:
            candidates = fetch_candidates(wh, window=args.window)
            passed, dropped = prefilter(candidates)
            kept = apply_quota(passed, profile_keywords(load_profile()), limit=args.limit)
            print(
                f"{len(candidates)} fetched · {len(dropped)} filtered · "
                f"{len(kept)} to score (quota {PER_SOURCE_QUOTA}/source)"
            )
            counts: dict[str, int] = defaultdict(int)
            for item in kept:
                counts[item["source_name"]] += 1
            for source, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {source:<22} {n:>3}")
            return 0

        result = enrich_pending(
            wh, limit=args.limit, window=args.window, backend_name=args.backend
        )
        print(result.summary())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
