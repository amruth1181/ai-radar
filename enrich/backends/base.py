"""The interface both providers implement.

Everything downstream — run.py, the dbt models, the digest — is provider-agnostic.
Switching from GLM to Claude is one environment variable, not a rewrite. That matters
because the decision depends on data you do not have yet: how often each backend
returns malformed JSON, and whether its scores match your judgement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


class EnrichmentAuthError(RuntimeError):
    """The provider rejected our credentials.

    Kept apart from every other failure because it behaves differently: a malformed
    response or a rate limit is transient and worth tolerating, but a bad key fails
    identically every day until someone changes it.

    It is also the most dangerous failure this pipeline has. The digest is assembled
    from items enriched on PREVIOUS runs, so a dead key produces a completely
    normal-looking digest -- ten items, right scores, nothing obviously wrong -- while
    silently ingesting nothing new. It only becomes visible days later when the last
    enriched item ages out of the 26-hour window. Verified in production: the key was
    set to a junk value and the run went green with a full digest.
    """


def first_text_block(message) -> str:
    """Return the first text block's content, or "" if there is none.

    Never index content[0] blindly. GLM-4.5-Flash returns a thinking block first and
    the answer second, so content[0].text raises AttributeError. Any model with
    reasoning enabled behaves the same way.
    """
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


@dataclass
class EnrichmentResult:
    """What a backend produced, plus what it cost to find out.

    Failures are counted by kind, because they mean opposite things. Malformed JSON is
    a model-quality problem and an argument for switching backend. Rate limiting is a
    throughput problem and an argument for lowering concurrency. Reporting both as one
    number sends you to fix the wrong thing.
    """

    rows: list[dict] = field(default_factory=list)
    attempted: int = 0
    malformed: int = 0
    rate_limited: int = 0
    errored: int = 0

    @property
    def failed(self) -> int:
        return self.malformed + self.rate_limited + self.errored

    @property
    def malformed_rate(self) -> float:
        """The number that decides whether a backend's quality is good enough."""
        return self.malformed / self.attempted if self.attempted else 0.0

    def summary(self) -> str:
        parts = [f"{len(self.rows)}/{self.attempted} enriched"]
        if self.malformed:
            parts.append(f"{self.malformed} malformed ({self.malformed_rate:.0%})")
        if self.rate_limited:
            parts.append(f"{self.rate_limited} rate-limited")
        if self.errored:
            parts.append(f"{self.errored} errored")
        return " · ".join(parts)


class EnrichmentBackend(ABC):
    """One item per request; results keyed by url_hash."""

    name: str = "base"
    model: str = ""

    @abstractmethod
    def enrich(self, items: list[dict], system_prompt: str) -> EnrichmentResult:
        """Score every item. Must never raise for a single bad item."""

    def _row(self, url_hash: str, payload: dict) -> dict:
        return {
            "url_hash": url_hash,
            **payload,
            "model": self.model,
            "enriched_at": datetime.now(timezone.utc),
        }
