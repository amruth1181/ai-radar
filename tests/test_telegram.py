"""MarkdownV2 escaping tests.

Telegram rejects the whole message on a single unescaped reserved character, and the
failure is a 400 with no partial delivery — so the digest just never arrives. These
tests are cheap insurance against that.
"""

from deliver.telegram import MAX_MESSAGE_CHARS, chunk, escape_md, link


class TestEscaping:
    def test_period_and_hyphen(self):
        """The plan's named case: a real title with both '.' and '-' in it."""
        assert escape_md("dbt 1.9 — zero-copy") == "dbt 1\\.9 — zero\\-copy"

    def test_every_reserved_character_is_escaped(self):
        for ch in "_*[]()~`>#+-=|{}.!":
            assert escape_md(ch) == f"\\{ch}", f"{ch!r} was not escaped"

    def test_backslash_is_escaped(self):
        assert escape_md("a\\b") == "a\\\\b"

    def test_ordinary_text_untouched(self):
        assert escape_md("Attention Is All You Need") == "Attention Is All You Need"

    def test_non_string_input_is_coerced(self):
        assert escape_md(9.2) == "9\\.2"


class TestLink:
    def test_label_escaped_url_left_usable(self):
        assert (
            link("GPT-4.5", "https://example.com/a_b")
            == "[GPT\\-4\\.5](https://example.com/a_b)"
        )

    def test_closing_paren_in_url_is_escaped(self):
        """An unescaped ')' would terminate the link early and corrupt the message."""
        assert link("wiki", "https://e.com/A_(b)") == "[wiki](https://e.com/A_(b\\))"


class TestChunk:
    def test_short_message_is_one_chunk(self):
        assert chunk("hello") == ["hello"]

    def test_long_message_splits_under_the_limit(self):
        text = "\n".join(f"line {i}" for i in range(2000))
        parts = chunk(text)
        assert len(parts) > 1
        assert all(len(p) <= MAX_MESSAGE_CHARS for p in parts)

    def test_split_preserves_all_lines_and_never_cuts_mid_line(self):
        text = "\n".join(f"line {i}" for i in range(2000))
        parts = chunk(text)
        rejoined = "\n".join(parts).split("\n")
        assert rejoined == text.split("\n")

    def test_escape_sequences_are_never_split(self):
        """A chunk ending on a lone '\\' would corrupt the next message."""
        text = "\n".join(escape_md(f"item-{i}.") for i in range(2000))
        assert not any(p.endswith("\\") for p in chunk(text))
