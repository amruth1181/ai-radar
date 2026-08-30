"""Tests for the shared item shape.

Every source funnels through build_item, so a bug here corrupts all four at once.
"""

from datetime import datetime, timezone

import pytest

from ingest.normalize import url_hash
from ingest.sources._common import (
    SUMMARY_MAX_CHARS,
    build_item,
    utc_from_epoch,
    utc_from_iso,
)

WHEN = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def make(**overrides):
    defaults = dict(
        source_name="test_src",
        source_type="rss",
        source_weight=1.0,
        external_id="abc123",
        url="https://example.com/post",
        title="A title",
        published_at=WHEN,
    )
    return build_item(**{**defaults, **overrides})


class TestBuildItem:
    def test_url_is_canonicalized_and_hashed_consistently(self):
        item = make(url="http://www.example.com/post/?utm_source=hn")
        assert item["url"] == "https://example.com/post"
        assert item["url_hash"] == url_hash("https://example.com/post")

    def test_title_is_trimmed(self):
        assert make(title="  spaced  ")["title"] == "spaced"

    def test_none_title_does_not_crash(self):
        assert make(title=None)["title"] == ""

    def test_summary_html_is_stripped(self):
        assert make(summary="<p>Hello <b>world</b></p>")["summary_raw"] == "Hello world"

    def test_summary_is_capped(self):
        assert len(make(summary="x" * 99_999)["summary_raw"]) == SUMMARY_MAX_CHARS

    def test_missing_summary_becomes_empty_string(self):
        assert make(summary=None)["summary_raw"] == ""

    def test_external_id_coerced_to_string(self):
        """GitHub returns an integer repo id; the column is text."""
        assert make(external_id=12345)["external_id"] == "12345"

    def test_optional_fields_default_to_none(self):
        item = make()
        assert item["discussion_url"] is None
        assert item["engagement"] is None
        assert item["author"] is None

    def test_fetched_at_is_tz_aware(self):
        assert make()["fetched_at"].tzinfo is not None

    def test_every_source_produces_identical_keys(self):
        """A missing key in one source would break the shared items schema."""
        rss = make(source_type="rss")
        hn = make(
            source_type="hackernews",
            discussion_url="https://news.ycombinator.com/item?id=1",
            engagement={"points": 10, "comments": 2},
        )
        assert rss.keys() == hn.keys()


class TestTimestamps:
    def test_epoch_is_utc_aware(self):
        parsed = utc_from_epoch(1787794392)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0

    def test_iso_with_z_suffix(self):
        assert utc_from_iso("2026-08-30T12:00:00Z") == WHEN

    def test_iso_with_offset_is_converted_to_utc(self):
        assert utc_from_iso("2026-08-30T14:00:00+02:00") == WHEN

    def test_naive_iso_is_assumed_utc(self):
        """Better a documented assumption than a naive datetime reaching dlt."""
        assert utc_from_iso("2026-08-30T12:00:00") == WHEN

    @pytest.mark.parametrize("value", [1787794392, 1787794392.5, "1787794392"])
    def test_epoch_accepts_numeric_forms(self, value):
        assert utc_from_epoch(value).tzinfo is not None
