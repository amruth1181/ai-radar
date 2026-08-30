"""Enrichment parsing and pre-filter tests.

Nothing here calls an API. The value is in the failure paths: a model that returns
prose, fences, or an out-of-range score must degrade to a skipped item, never to a
crashed run or a row that breaks a dbt test downstream.
"""

import pytest

from enrich.prompts import CATEGORIES, build_system, build_user, parse_response
from enrich.run import _mostly_ascii, prefilter


class TestParseResponse:
    def valid(self, **overrides):
        payload = {
            "summary": "A thing shipped. It does something.",
            "category": "tooling",
            "entities": ["dbt", "DuckDB"],
            "relevance_score": 8,
            "reason": "matches dbt interest",
        }
        payload.update(overrides)
        import json

        return json.dumps(payload)

    def test_clean_json(self):
        result = parse_response(self.valid())
        assert result["relevance_score"] == 8
        assert result["category"] == "tooling"
        assert result["entities"] == ["dbt", "DuckDB"]

    def test_markdown_fences_are_stripped(self):
        assert parse_response(f"```json\n{self.valid()}\n```")["relevance_score"] == 8

    def test_preamble_prose_is_tolerated(self):
        text = f"Sure, here is the JSON you asked for:\n{self.valid()}"
        assert parse_response(text)["relevance_score"] == 8

    @pytest.mark.parametrize("bad", ["", "not json", "{broken", None, "[]"])
    def test_unusable_input_returns_none(self, bad):
        assert parse_response(bad) is None

    def test_missing_score_is_rejected(self):
        """Without a score the item cannot be ranked, so it is not worth keeping."""
        assert parse_response('{"summary": "x", "category": "tooling"}') is None

    @pytest.mark.parametrize("raw,expected", [(99, 10), (-4, 0), ("7", 7), (10, 10)])
    def test_score_is_clamped_not_rejected(self, raw, expected):
        """An 11 would fail the dbt accepted_range test and break the whole build."""
        assert parse_response(self.valid(relevance_score=raw))["relevance_score"] == expected

    def test_unknown_category_falls_back_to_other(self):
        """Anything outside the list would fail the accepted_values test."""
        assert parse_response(self.valid(category="hallucinated"))["category"] == "other"

    def test_every_declared_category_survives(self):
        for category in CATEGORIES:
            assert parse_response(self.valid(category=category))["category"] == category

    def test_entities_capped_at_five(self):
        result = parse_response(self.valid(entities=list("abcdefghij")))
        assert len(result["entities"]) == 5

    def test_non_list_entities_become_empty(self):
        assert parse_response(self.valid(entities="dbt"))["entities"] == []


class TestPrefilter:
    def item(self, title, url_hash="h1"):
        return {"url_hash": url_hash, "title": title}

    def test_keeps_a_normal_title(self):
        kept, dropped = prefilter([self.item("A perfectly reasonable article title")])
        assert len(kept) == 1 and not dropped

    def test_drops_short_titles(self):
        kept, dropped = prefilter([self.item("Hi")])
        assert not kept and dropped[0][1] == "title too short"

    def test_drops_non_english(self):
        kept, dropped = prefilter([self.item("这是一篇关于大型语言模型的文章内容")])
        assert not kept and dropped[0][1] == "not English"

    def test_missing_title_does_not_crash(self):
        kept, dropped = prefilter([{"url_hash": "h", "title": None}])
        assert not kept and dropped

    def test_partitions_without_losing_items(self):
        items = [self.item("A good long title here", "a"), self.item("no", "b")]
        kept, dropped = prefilter(items)
        assert len(kept) + len(dropped) == len(items)


class TestAsciiGate:
    @pytest.mark.parametrize("text", ["Plain English title", "GPT-4.5 released", "dbt 1.9"])
    def test_english_passes(self, text):
        assert _mostly_ascii(text)

    @pytest.mark.parametrize("text", ["这是一篇关于大型语言模型", "", "日本語のタイトルです"])
    def test_non_english_and_empty_fail(self, text):
        assert not _mostly_ascii(text)

    def test_accents_still_pass(self):
        """A few non-ASCII characters must not disqualify an English title."""
        assert _mostly_ascii("Café culture and LLM inference benchmarks")


class TestPromptBuilding:
    def test_profile_is_embedded_verbatim(self):
        cfg = {
            "profile": "I am a data engineer.",
            "high_interest": ["dbt", "RAG"],
            "low_interest": ["funding rounds"],
        }
        prompt = build_system(cfg)
        assert "I am a data engineer." in prompt
        assert "- dbt" in prompt and "- funding rounds" in prompt
        # The category list must reach the model or it will invent its own.
        assert "model_release" in prompt

    def test_user_prompt_truncates_long_content(self):
        item = {
            "title": "T",
            "source_name": "s",
            "published_at": "2026-08-30",
            "summary_raw": "x" * 99_999,
        }
        assert len(build_user(item)) < 3000
