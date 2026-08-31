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
import random
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic

import settings
from enrich.backends.base import (
    EnrichmentAuthError,
    EnrichmentBackend,
    EnrichmentResult,
    first_text_block,
)
from enrich.prompts import build_user, parse_response

log = logging.getLogger(__name__)

BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_MODEL = "glm-4.5-flash"
# The free tier allows roughly one request per second. Five workers walked straight
# through that and lost 40% of a five-item trial to 429s. Two workers plus backoff
# keeps throughput without tripping it -- and at ~60 items a day, wall-clock time
# for this step is irrelevant anyway.
MAX_WORKERS = 2
MAX_TOKENS = 400

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0


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

    def _one(self, item: dict, system_prompt: str) -> tuple[dict | None, str]:
        """Score one item. Returns (row, kind) where kind is ok/malformed/rate_limited/errored."""
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": build_user(item)}],
                    # Reasoning is on by default and is pure waste for classification:
                    # a two-character answer cost 263 output tokens with it enabled
                    # and 3 without. It also crowds max_tokens and truncates the JSON.
                    thinking={"type": "disabled"},
                )
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                # No point retrying, and no point attempting the other 60 items: they
                # will all fail identically. Abort the whole step instead.
                raise EnrichmentAuthError(
                    f"GLM rejected the API key: {str(exc)[:120]}"
                ) from exc
            except anthropic.RateLimitError:
                if attempt == MAX_RETRIES - 1:
                    log.warning("glm rate limited for %s, giving up", item["url_hash"])
                    return None, "rate_limited"
                # Jittered exponential backoff: without jitter the whole worker pool
                # retries in lockstep and trips the limit again together.
                delay = BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(0, 1)
                log.info("rate limited, retrying in %.1fs", delay)
                time.sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
                log.warning("glm failed for %s: %s", item["url_hash"], exc)
                return None, "errored"

            payload = parse_response(first_text_block(response))
            if payload is None:
                log.warning("glm returned unparseable JSON for %s", item["url_hash"])
                return None, "malformed"
            return self._row(item["url_hash"], payload), "ok"

        return None, "rate_limited"

    def enrich(self, items: list[dict], system_prompt: str) -> EnrichmentResult:
        result = EnrichmentResult(attempted=len(items))
        if not items:
            return result

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for row, kind in pool.map(lambda it: self._one(it, system_prompt), items):
                if kind == "ok":
                    result.rows.append(row)
                else:
                    setattr(result, kind, getattr(result, kind) + 1)
        return result
