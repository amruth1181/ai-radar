"""Backend selection.

One environment variable decides which provider runs. Everything downstream is
provider-agnostic, so this is the entire switching cost.
"""

from __future__ import annotations

import settings
from enrich.backends.base import EnrichmentBackend, EnrichmentResult

__all__ = ["EnrichmentBackend", "EnrichmentResult", "get_backend"]


def get_backend(name: str | None = None) -> EnrichmentBackend:
    name = (name or settings.get("ENRICH_BACKEND", "glm") or "glm").lower()

    if name == "glm":
        from enrich.backends.glm import GLMBackend

        return GLMBackend()
    if name == "claude":
        from enrich.backends.claude import ClaudeBackend

        return ClaudeBackend()

    raise ValueError(f"unknown ENRICH_BACKEND '{name}' (expected 'glm' or 'claude')")
