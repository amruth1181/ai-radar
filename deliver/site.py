"""Render the public archive.

The archive is **what was actually delivered**, not everything ever scored. It reads
raw.sent_items joined to fct_items, so a page can only contain items that reached a
channel -- which is what makes it an honest record rather than a dump of the warehouse.

Deliberately static. The build has warehouse credentials; the published page must not,
so nothing here emits JavaScript that talks to BigQuery and no key ever reaches the
output. Verify by opening site/index.html with networking off -- it should render
completely.

    uv run python -m deliver.site            # writes ./site
    uv run python -m deliver.site --out docs # somewhere else
"""

from __future__ import annotations

import argparse
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import settings
from warehouse import Warehouse, get_warehouse

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_OUT = settings.REPO_ROOT / "site"

REPO_URL = "https://github.com/amruth1181/ai-radar"

# Days rendered in full on the index, so recent digests can be scrolled rather than
# clicked through. Older days stay one click away — inlining a year of history would
# make the front page megabytes.
INLINE_DAYS = 5

# One row per item per day it was delivered. A single item sent to two channels on the
# same day is one archive entry, hence the group by rather than a plain select.
ARCHIVE_SQL = """
select
    cast(s.sent_at as date) as sent_date,
    f.url_hash,
    max(f.title)           as title,
    max(f.url)             as url,
    max(f.summary)         as summary,
    max(f.category)        as category,
    max(f.reason)          as reason,
    max(f.seen_in)         as seen_in,
    max(f.source_name)     as source_name,
    max(f.discussion_url)  as discussion_url,
    max(f.relevance_score) as relevance_score,
    max(f.final_score)     as final_score
from {sent} s
join {fct} f on s.url_hash = f.url_hash
group by 1, 2
order by 1 desc, max(f.final_score) desc
"""


@dataclass
class Item:
    title: str
    url: str
    summary: str
    category: str
    reason: str
    seen_in: str
    discussion_url: str | None
    relevance_score: int
    final_score: float


@dataclass
class Day:
    day: date
    items: list[Item]

    @property
    def slug(self) -> str:
        return self.day.isoformat()

    @property
    def label(self) -> str:
        return self.day.strftime("%A %-d %B %Y")

    @property
    def short(self) -> str:
        return self.day.strftime("%-d %b")

    @property
    def categories(self) -> list[str]:
        seen = []
        for item in self.items:
            if item.category not in seen:
                seen.append(item.category)
        return seen


def fetch_archive(wh: Warehouse) -> list[Day]:
    """Every delivered item, newest day first."""
    rows = wh.query(
        ARCHIVE_SQL.format(
            sent=wh.table("raw", "sent_items"),
            fct=wh.table("analytics", "fct_items"),
        )
    )

    grouped: dict[date, list[Item]] = defaultdict(list)
    for row in rows:
        day = row["sent_date"]
        if isinstance(day, datetime):
            day = day.date()
        grouped[day].append(
            Item(
                title=row["title"] or "",
                url=row["url"],
                summary=row["summary"] or "",
                category=row["category"] or "other",
                reason=row["reason"] or "",
                seen_in=row["seen_in"] or row["source_name"] or "",
                discussion_url=row["discussion_url"],
                relevance_score=int(row["relevance_score"] or 0),
                final_score=float(row["final_score"] or 0.0),
            )
        )

    return [Day(day=d, items=items) for d, items in sorted(grouped.items(), reverse=True)]


def render(days: list[Day], out_dir: Path) -> list[Path]:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    context = {
        "repo_url": REPO_URL,
        "built_at": datetime.now(timezone.utc),
        "total_items": sum(len(d.items) for d in days),
        "total_days": len(days),
        "inline_days": INLINE_DAYS,
    }

    written = []

    index = out_dir / "index.html"
    index.write_text(
        env.get_template("index.html").render(
            days=days, **context
        )
    )
    written.append(index)

    day_template = env.get_template("day.html")
    for position, day in enumerate(days):
        page = out_dir / f"{day.slug}.html"
        page.write_text(
            day_template.render(
                day=day,
                newer=days[position - 1] if position > 0 else None,
                older=days[position + 1] if position + 1 < len(days) else None,
                **context,
            )
        )
        written.append(page)

    # Tells GitHub Pages not to run the output through Jekyll, which would otherwise
    # ignore files and directories beginning with an underscore.
    (out_dir / ".nojekyll").write_text("")

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the static archive.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    with get_warehouse() as wh:
        days = fetch_archive(wh)

    written = render(days, out_dir)
    print(f"{len(written)} pages -> {out_dir}")
    print(f"  {sum(len(d.items) for d in days)} items across {len(days)} day(s)")
    if not days:
        print("  (no deliveries recorded yet — the index explains that)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
