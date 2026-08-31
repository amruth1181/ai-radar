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


class TestFirstTextBlock:
    """GLM-4.5-Flash returns a thinking block first and the answer second.

    Indexing content[0] blindly raised AttributeError against the live API — this is
    that bug pinned down.
    """

    class Block:
        def __init__(self, type_, **kw):
            self.type = type_
            for k, v in kw.items():
                setattr(self, k, v)

    class Message:
        def __init__(self, content):
            self.content = content

    def test_skips_leading_thinking_block(self):
        from enrich.backends.base import first_text_block

        msg = self.Message(
            [
                self.Block("thinking", thinking="a long chain of reasoning"),
                self.Block("text", text='{"relevance_score": 8}'),
            ]
        )
        assert first_text_block(msg) == '{"relevance_score": 8}'

    def test_plain_text_first_still_works(self):
        from enrich.backends.base import first_text_block

        assert first_text_block(self.Message([self.Block("text", text="OK")])) == "OK"

    def test_no_text_block_returns_empty_not_crash(self):
        from enrich.backends.base import first_text_block

        msg = self.Message([self.Block("thinking", thinking="only reasoning")])
        assert first_text_block(msg) == ""

    def test_empty_content_returns_empty(self):
        from enrich.backends.base import first_text_block

        assert first_text_block(self.Message([])) == ""


class TestFailureAccounting:
    """Malformed JSON and rate limiting mean opposite things and must not be conflated."""

    def test_kinds_are_counted_separately(self):
        from enrich.backends.base import EnrichmentResult

        r = EnrichmentResult(attempted=10, malformed=1, rate_limited=4, errored=1)
        assert r.failed == 6
        # Only malformed reflects model quality; rate limiting is a throughput problem.
        assert r.malformed_rate == 0.1

    def test_rate_limits_do_not_inflate_the_quality_metric(self):
        from enrich.backends.base import EnrichmentResult

        r = EnrichmentResult(attempted=5, rate_limited=5)
        assert r.malformed_rate == 0.0

    def test_zero_attempts_does_not_divide_by_zero(self):
        from enrich.backends.base import EnrichmentResult

        assert EnrichmentResult().malformed_rate == 0.0


class TestAuthFailureIsFatal:
    """A rejected credential must stop the run, not degrade quietly.

    Found in production: the GLM key was set to a junk value and the daily workflow
    went green with a full 10-item digest. The digest is assembled from items enriched
    on PREVIOUS runs, so a dead key looks completely normal -- nothing new is scored,
    and it stays invisible until the last enriched item ages out of the 26-hour window
    days later.
    """

    def test_auth_error_is_not_a_generic_runtime_error_to_callers(self):
        from enrich.backends.base import EnrichmentAuthError

        # Subclasses RuntimeError so existing handlers still catch it, but callers can
        # single it out for different treatment.
        assert issubclass(EnrichmentAuthError, RuntimeError)

    def test_orchestrator_converts_it_to_a_hard_stop(self, monkeypatch):
        import scripts.run_daily as run_daily
        from enrich.backends.base import EnrichmentAuthError

        monkeypatch.setattr(
            "enrich.run.enrich_pending",
            lambda *a, **k: (_ for _ in ()).throw(EnrichmentAuthError("bad key")),
        )
        with pytest.raises(run_daily.StepFailed):
            run_daily.step_enrich()

    def test_other_failures_do_not_stop_the_run(self, monkeypatch):
        """Malformed JSON and rate limits stay soft: a partial digest still ships."""
        import scripts.run_daily as run_daily
        from enrich.backends.base import EnrichmentResult

        monkeypatch.setattr(
            "enrich.run.enrich_pending",
            lambda *a, **k: EnrichmentResult(attempted=5, malformed=2, rate_limited=1),
        )
        assert run_daily.step_enrich() == 3
