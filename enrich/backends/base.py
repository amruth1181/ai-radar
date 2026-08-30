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


@dataclass
class EnrichmentResult:
    """What a backend produces, plus what it cost to find out."""

    rows: list[dict] = field(default_factory=list)
    attempted: int = 0
    failed: int = 0

    @property
    def failure_rate(self) -> float:
        """The number that decides whether a backend is good enough."""
        return self.failed / self.attempted if self.attempted else 0.0

    def summary(self) -> str:
        return (
            f"{len(self.rows)}/{self.attempted} enriched"
            + (f" · {self.failed} failed ({self.failure_rate:.0%})" if self.failed else "")
        )


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
