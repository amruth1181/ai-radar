"""Discord formatting tests.

Discord is more forgiving than Telegram — a stray character degrades formatting rather
than getting the message rejected — but embed limits are hard: exceed them and the API
returns 400 and nothing is delivered.
"""

from deliver.discord import (
    CATEGORY_COLORS,
    MAX_CONTENT_CHARS,
    MAX_EMBED_DESCRIPTION,
    build_embed,
    chunk,
    escape_md,
    link,
)


class TestEscaping:
    def test_formatting_characters_escaped(self):
        for ch in "*_~`|\\":
            assert escape_md(ch) == f"\\{ch}", f"{ch!r} was not escaped"

    def test_source_name_underscores(self):
        """simon_willison would otherwise render as italic 'simon willison'."""
        assert escape_md("simon_willison") == "simon\\_willison"

    def test_periods_and_hyphens_left_alone(self):
        """Unlike Telegram MarkdownV2, these are not special in Discord."""
        assert escape_md("dbt 1.9 — zero-copy") == "dbt 1.9 — zero-copy"

    def test_non_string_input_coerced(self):
        assert escape_md(9.2) == "9.2"


class TestLink:
    def test_plain_link(self):
        assert link("Hy4 Preview", "https://e.com/a") == "[Hy4 Preview](https://e.com/a)"

    def test_bracket_in_label_escaped(self):
        """An unescaped ']' would end the label early and break the link."""
        assert link("a]b", "https://e.com") == "[a\\]b](https://e.com)"

    def test_parens_in_url_encoded(self):
        """A ')' would terminate the link; percent-encoding keeps the URL valid."""
        assert link("wiki", "https://e.com/A_(b)") == "[wiki](https://e.com/A_%28b%29)"

    def test_underscore_in_label_escaped_but_url_untouched(self):
        assert link("a_b", "https://e.com/a_b") == "[a\\_b](https://e.com/a_b)"


class TestChunk:
    def test_short_message_is_one_chunk(self):
        assert chunk("hello") == ["hello"]

    def test_respects_discord_2000_char_limit(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        parts = chunk(text)
        assert len(parts) > 1
        assert all(len(p) <= MAX_CONTENT_CHARS for p in parts)

    def test_no_lines_lost_in_split(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        assert "\n".join(chunk(text)).split("\n") == text.split("\n")


class TestEmbed:
    def test_category_drives_colour(self):
        embed = build_embed("t", "https://e.com", "d", "f", category="model_release")
        assert embed["color"] == CATEGORY_COLORS["model_release"]

    def test_unknown_category_falls_back(self):
        embed = build_embed("t", "https://e.com", "d", "f", category="nonsense")
        assert embed["color"] == CATEGORY_COLORS["other"]

    def test_title_truncated_to_api_limit(self):
        embed = build_embed("x" * 500, "https://e.com", "d", "f")
        assert len(embed["title"]) == 256

    def test_description_truncated_to_api_limit(self):
        embed = build_embed("t", "https://e.com", "y" * 9000, "f")
        assert len(embed["description"]) == MAX_EMBED_DESCRIPTION

    def test_url_is_preserved_verbatim(self):
        """The embed `url` field is not markdown — it must not be escaped."""
        url = "https://e.com/a_b?x=1&y=2"
        assert build_embed("t", url, "d", "f")["url"] == url
