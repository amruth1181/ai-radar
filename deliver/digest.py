"""The digest every channel renders.

One query, one shape. Discord, Gmail and the static site are formatters over this --
no channel writes its own SQL, so they can never disagree about what today's digest is.

Also owns the sent-items ledger. Without it the 26-hour window would resend yesterday's
top item, which is the fastest way to stop trusting the digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from warehouse import Warehouse, get_warehouse


@dataclass
class DigestItem:
    url_hash: str
    title: str
    url: str
    summary: str
    category: str
    reason: str
    source_name: str
    seen_in: str
    relevance_score: int
    final_score: float
    discussion_url: str | None = None
    max_points: int | None = None
    corroboration: int = 1

    @property
    def corroborated(self) -> bool:
        """Surfaced independently by more than one source."""
        return self.corroboration > 1


@dataclass
class Digest:
    """Today's items plus the run stats that go in the footer."""

    items: list[DigestItem] = field(default_factory=list)
    scanned: int = 0
    enriched: int = 0
    unscored: int = 0
    failed_sources: list[tuple[str, str]] = field(default_factory=list)
    built_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_quiet(self) -> bool:
        """Few enough items that the message should say so explicitly."""
        return len(self.items) < 3

    def by_category(self) -> dict[str, list[DigestItem]]:
        grouped: dict[str, list[DigestItem]] = {}
        for item in self.items:
            grouped.setdefault(item.category or "other", []).append(item)
        return grouped

    def footer(self) -> str:
        """The line you actually read every day. This is the real dashboard."""
        parts = [f"{self.scanned} scanned", f"{len(self.items)} shown"]
        if self.unscored:
            parts.append(f"⚠️ {self.unscored} unscored")
        for name, reason in self.failed_sources:
            parts.append(f"⚠️ {name} failed ({reason[:40]})")
        return " · ".join(parts)


DIGEST_SQL = """
select
    url_hash, title, url, discussion_url, summary, category, reason,
    source_name, seen_in, corroboration, max_points,
    relevance_score, final_score
from {digest}
order by final_score desc
"""

STATS_SQL = """
select
    (select count(*) from {items})       as scanned,
    (select count(*) from {enrichments}) as enriched,
    (select count(*) from {fct}
      where relevance_score is null
        and published_at >= {cutoff})    as unscored
"""


def build_digest(
    wh: Warehouse | None = None,
    failed_sources: list[tuple[str, str]] | None = None,
) -> Digest:
    """Read today's digest and run stats. The only place that queries for delivery."""
    owned = wh is None
    wh = wh or get_warehouse()
    try:
        rows = wh.query(DIGEST_SQL.format(digest=wh.table("analytics", "fct_daily_digest")))

        cutoff = (
            "now() - interval 26 hour"
            if wh.target == "dev"
            else "timestamp_sub(current_timestamp(), interval 26 hour)"
        )
        stats = wh.query(
            STATS_SQL.format(
                items=wh.table("raw", "items"),
                enrichments=wh.table("raw", "enrichments"),
                fct=wh.table("analytics", "fct_items"),
                cutoff=cutoff,
            )
        )[0]

        return Digest(
            items=[DigestItem(**_clean(row)) for row in rows],
            scanned=stats["scanned"],
            enriched=stats["enriched"],
            unscored=stats["unscored"],
            failed_sources=failed_sources or [],
        )
    finally:
        if owned:
            wh.close()


def _clean(row: dict) -> dict:
    """Coerce warehouse types into the dataclass's shape."""
    return {
        "url_hash": row["url_hash"],
        "title": row["title"] or "",
        "url": row["url"],
        "discussion_url": row.get("discussion_url"),
        "summary": row.get("summary") or "",
        "category": row.get("category") or "other",
        "reason": row.get("reason") or "",
        "source_name": row.get("source_name") or "",
        "seen_in": row.get("seen_in") or "",
        "corroboration": int(row.get("corroboration") or 1),
        "max_points": int(row["max_points"]) if row.get("max_points") else None,
        "relevance_score": int(row.get("relevance_score") or 0),
        "final_score": float(row.get("final_score") or 0.0),
    }


def record_sent(digest: Digest, channel: str, wh: Warehouse | None = None) -> int:
    """Append delivered items to the ledger.

    Called only after a channel confirms delivery. Recording before sending would
    silently drop a digest whose send failed -- the items would never be eligible again.
    """
    if not digest.items:
        return 0

    owned = wh is None
    wh = wh or get_warehouse()
    try:
        now = datetime.now(timezone.utc)
        return wh.insert(
            "raw",
            "sent_items",
            [
                {"url_hash": item.url_hash, "channel": channel, "sent_at": now}
                for item in digest.items
            ],
        )
    finally:
        if owned:
            wh.close()
