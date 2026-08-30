"""URL canonicalization and hashing.

This is the single most important module in the project. Bad canonicalization means
duplicates in the digest, which is the failure mode that makes you stop reading it.

Every duplicate that ever escapes into the digest should become a new case in
tests/test_normalize.py. Fix `canonicalize`; never loosen the dbt unique test.
"""

from __future__ import annotations

import hashlib
import re
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query params that identify the referrer, not the resource. Stripping these collapses
# the same article shared from Twitter, a newsletter, and Google into one row.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "s",  # twitter share suffix, e.g. ?s=20
    }
)

# Hosts that serve identical content under different names.
NETLOC_ALIASES = {
    "export.arxiv.org": "arxiv.org",
    "xxx.lanl.gov": "arxiv.org",
    "m.youtube.com": "youtube.com",
}

DEFAULT_PORTS = {"80", "443"}

# arXiv appends a version suffix to the final path segment: 2401.12345v3.
# Anchored to the end and requiring digits so it cannot misfire on a literal "v"
# elsewhere in the identifier (e.g. the old-style "cond-mat/9910348").
_ARXIV_VERSION = re.compile(r"v\d+$")

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _normalize_netloc(netloc: str) -> str:
    netloc = netloc.lower().removeprefix("www.")
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if port in DEFAULT_PORTS:
            netloc = host
    return NETLOC_ALIASES.get(netloc, netloc)


def _normalize_arxiv_path(path: str) -> str:
    """Collapse /pdf/, .pdf and version suffixes onto the canonical /abs/ form."""
    path = path.replace("/pdf/", "/abs/")
    path = path.removesuffix(".pdf")
    head, sep, tail = path.rpartition("/")
    if sep:
        path = f"{head}/{_ARXIV_VERSION.sub('', tail)}"
    return path


def canonicalize(url: str) -> str:
    """Return a stable, comparable form of `url`.

    Forces https, drops `www.`, resolves known host aliases, strips tracking params,
    sorts the remaining query, removes the fragment, and applies arXiv-specific rules.
    """
    parsed = urlparse(url.strip())
    netloc = _normalize_netloc(parsed.netloc)
    path = parsed.path.rstrip("/")

    if netloc == "arxiv.org":
        path = _normalize_arxiv_path(path)

    # Sorted, so ?a=1&b=2 and ?b=2&a=1 produce one hash.
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if k.lower() not in TRACKING_PARAMS
        )
    )
    return urlunparse(("https", netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """16-char digest of the canonical URL. The join key used everywhere downstream."""
    return hashlib.sha256(canonicalize(url).encode()).hexdigest()[:16]


def strip_html(text: str | None) -> str:
    """Flatten feed summary HTML to plain text. Feeds are wildly inconsistent here."""
    if not text:
        return ""
    return _WHITESPACE.sub(" ", unescape(_TAG.sub(" ", text))).strip()
