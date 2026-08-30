"""GLM via its Anthropic-compatible endpoint.

z.ai exposes an Anthropic-shaped API, so the official `anthropic` SDK works unchanged
with a base_url swap. That is the whole reason this backend is ~60 lines rather than a
second HTTP client.

No Batch API here, so requests go out concurrently instead. Concurrency is bounded:
a free tier will rate-limit long before it runs out of capacity, and a 429 storm costs
more time than the concurrency saves.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import anthropic

import settings
from enrich.backends.base import EnrichmentBackend, EnrichmentResult
from enrich.prompts import build_user, parse_response

log = logging.getLogger(__name__)

BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_MODEL = "glm-4.5-flash"
MAX_WORKERS = 5
MAX_TOKENS = 400


class GLMBackend(EnrichmentBackend):
    name = "glm"

    def __init__(self, model: str | None = None):
        api_key = settings.get("GLM_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GLM_API_KEY is not set. Get a key from z.ai, or switch to the paid "
                "backend with ENRICH_BACKEND=claude."
            )
        self.model = model or settings.get("GLM_MODEL") or DEFAULT_MODEL
        self._client = anthropic.Anthropic(api_key=api_key, base_url=BASE_URL)

    def _one(self, item: dict, system_prompt: str) -> dict | None:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": build_user(item)}],
            )
            payload = parse_response(response.content[0].text)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            log.warning("glm failed for %s: %s", item["url_hash"], exc)
            return None

        if payload is None:
            log.warning("glm returned unparseable JSON for %s", item["url_hash"])
            return None
        return self._row(item["url_hash"], payload)

    def enrich(self, items: list[dict], system_prompt: str) -> EnrichmentResult:
        result = EnrichmentResult(attempted=len(items))
        if not items:
            return result

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for row in pool.map(lambda it: self._one(it, system_prompt), items):
                if row is None:
                    result.failed += 1
                else:
                    result.rows.append(row)
        return result
