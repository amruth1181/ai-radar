"""Canonicalization tests — the project's duplicate alarm.

When a duplicate escapes into the digest, add the offending pair here first, then fix
`canonicalize` until this file passes again.
"""

import pytest

from ingest.normalize import canonicalize, strip_html, url_hash


def hashes(*urls: str) -> set[str]:
    return {url_hash(u) for u in urls}


class TestArxiv:
    def test_variants_collapse(self):
        """The bug that bites everyone: one paper, five URL shapes."""
        assert (
            len(
                hashes(
                    "https://arxiv.org/abs/2401.12345",
                    "http://arxiv.org/abs/2401.12345v2",
                    "https://www.arxiv.org/pdf/2401.12345.pdf",
                    "https://arxiv.org/abs/2401.12345?utm_source=twitter",
                    "https://export.arxiv.org/abs/2401.12345v11/",
                )
            )
            == 1
        )

    def test_old_style_id_with_v_in_category_survives(self):
        """`cond-mat/9910348` has no version suffix — nothing may be stripped."""
        assert canonicalize("https://arxiv.org/abs/cond-mat/9910348").endswith(
            "/abs/cond-mat/9910348"
        )

    def test_old_style_id_version_is_stripped(self):
        assert canonicalize("https://arxiv.org/abs/cond-mat/9910348v3").endswith(
            "/abs/cond-mat/9910348"
        )

    def test_distinct_papers_stay_distinct(self):
        assert len(
            hashes(
                "https://arxiv.org/abs/2401.12345",
                "https://arxiv.org/abs/2401.12346",
            )
        ) == 2


class TestGeneric:
    def test_scheme_www_and_trailing_slash(self):
        assert (
            len(
                hashes(
                    "http://example.com/post",
                    "https://example.com/post",
                    "https://www.example.com/post/",
                    "https://WWW.EXAMPLE.COM/post",
                )
            )
            == 1
        )

    def test_default_ports_are_dropped(self):
        assert len(
            hashes("https://example.com:443/post", "https://example.com/post")
        ) == 1

    def test_fragment_is_dropped(self):
        assert len(
            hashes("https://example.com/post#section-2", "https://example.com/post")
        ) == 1

    def test_tracking_params_stripped(self):
        assert (
            len(
                hashes(
                    "https://example.com/post",
                    "https://example.com/post?utm_source=hn&utm_medium=social",
                    "https://example.com/post?fbclid=abc123",
                    "https://example.com/post?ref=newsletter&s=20",
                )
            )
            == 1
        )

    def test_meaningful_params_are_kept(self):
        """?id=5 identifies the resource — stripping it would merge distinct pages."""
        assert len(
            hashes("https://example.com/item?id=5", "https://example.com/item?id=6")
        ) == 2

    def test_query_order_does_not_matter(self):
        assert len(
            hashes(
                "https://example.com/s?a=1&b=2",
                "https://example.com/s?b=2&a=1",
            )
        ) == 1

    def test_path_case_is_significant(self):
        """Hosts are case-insensitive; paths are not."""
        assert len(
            hashes("https://example.com/Post", "https://example.com/post")
        ) == 2

    def test_surrounding_whitespace_ignored(self):
        assert len(
            hashes("  https://example.com/post \n", "https://example.com/post")
        ) == 1

    def test_hash_is_stable_and_short(self):
        h = url_hash("https://example.com/post")
        assert h == url_hash("https://example.com/post")
        assert len(h) == 16


class TestStripHtml:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("<p>Hello <b>world</b></p>", "Hello world"),
            ("Caf&eacute; &amp; bar", "Café & bar"),
            ("  spaced\n\nout  ", "spaced out"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_flattens_to_plain_text(self, raw, expected):
        assert strip_html(raw) == expected
