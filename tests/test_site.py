"""Static archive tests.

The important one is TestNothingLeaks. The build has warehouse credentials and the
published page does not, and that boundary is invisible in code review -- a stray
script tag or a fetched resource would only show up once the page was already public.
"""

import re
from datetime import date

import pytest

from deliver.site import Day, Item, render


def item(title="A Real Article Title", category="tooling", score=8, **kw):
    defaults = dict(
        title=title,
        url="https://example.com/post",
        summary="Something shipped. It does a thing.",
        category=category,
        reason="matches dbt interest",
        seen_in="simon_willison,hn_ai",
        discussion_url=None,
        relevance_score=score,
        final_score=6.6,
    )
    defaults.update(kw)
    return Item(**defaults)


@pytest.fixture
def site(tmp_path):
    days = [
        Day(day=date(2026, 8, 31), items=[item(), item("Second", "research", 7)]),
        Day(day=date(2026, 8, 30), items=[item("Older", "model_release", 9)]),
    ]
    render(days, tmp_path / "site")
    return tmp_path / "site"


class TestStructure:
    def test_writes_index_and_one_page_per_day(self, site):
        assert (site / "index.html").exists()
        assert (site / "2026-08-31.html").exists()
        assert (site / "2026-08-30.html").exists()

    def test_nojekyll_present(self, site):
        """Without it, GitHub Pages hides anything starting with an underscore."""
        assert (site / ".nojekyll").exists()

    def test_index_shows_the_newest_day(self, site):
        html = (site / "index.html").read_text()
        assert "31 August 2026" in html
        assert "A Real Article Title" in html

    def test_older_days_are_linked_not_inlined(self, site):
        html = (site / "index.html").read_text()
        assert "2026-08-30.html" in html
        assert "Older" not in html

    def test_day_pages_link_to_neighbours(self, site):
        newest = (site / "2026-08-31.html").read_text()
        oldest = (site / "2026-08-30.html").read_text()
        assert "2026-08-30.html" in newest  # back to older
        assert "2026-08-31.html" in oldest  # forward to newer

    def test_output_directory_is_replaced_not_merged(self, tmp_path):
        """A deleted day must not linger from a previous build."""
        out = tmp_path / "site"
        render([Day(day=date(2026, 8, 30), items=[item()])], out)
        assert (out / "2026-08-30.html").exists()
        render([Day(day=date(2026, 8, 31), items=[item()])], out)
        assert not (out / "2026-08-30.html").exists()

    def test_empty_archive_still_renders_an_index(self, tmp_path):
        """Before the first delivery the site must say so, not 500."""
        out = tmp_path / "site"
        render([], out)
        html = (out / "index.html").read_text()
        assert (out / "index.html").exists()
        assert "No digests delivered yet" in html


class TestNothingLeaks:
    def test_no_script_tags(self, site):
        for page in site.glob("*.html"):
            assert "<script" not in page.read_text().lower(), page.name

    def test_page_fetches_no_external_resources(self, site):
        """Renders offline. Anything fetched would also be a tracking vector."""
        for page in site.glob("*.html"):
            fetched = re.findall(
                r'(?:<script[^>]+src|<link[^>]+href|<img[^>]+src)="([^"]+)"',
                page.read_text(),
            )
            external = [u for u in fetched if u.startswith(("http", "//"))]
            assert external == [], f"{page.name} fetches {external}"

    def test_no_credential_shaped_strings(self, site):
        pattern = re.compile(
            r"(discord\.com/api/webhooks|BEGIN PRIVATE KEY|service_account"
            r"|GLM_API_KEY|ANTHROPIC_API_KEY|project-[0-9a-f]{8})",
            re.I,
        )
        for page in site.glob("*.html"):
            found = pattern.findall(page.read_text())
            assert found == [], f"{page.name} contains {found}"


class TestEscaping:
    def test_html_in_a_title_is_escaped(self, tmp_path):
        """Feed titles are untrusted input and land straight in the page."""
        out = tmp_path / "site"
        render([Day(day=date(2026, 8, 31),
                    items=[item(title="<script>alert(1)</script> Release")])], out)
        html = (out / "index.html").read_text()
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_ampersand_in_title_survives(self, tmp_path):
        out = tmp_path / "site"
        render([Day(day=date(2026, 8, 31), items=[item(title="R&D at Scale")])], out)
        assert "R&amp;D at Scale" in (out / "index.html").read_text()


class TestDay:
    def test_slug_is_iso(self):
        assert Day(day=date(2026, 8, 31), items=[]).slug == "2026-08-31"

    def test_categories_preserve_first_seen_order_without_duplicates(self):
        day = Day(day=date(2026, 8, 31), items=[
            item(category="tooling"), item(category="research"), item(category="tooling"),
        ])
        assert day.categories == ["tooling", "research"]
