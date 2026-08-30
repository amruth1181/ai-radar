"""Claude Haiku 4.5 via the Batch API.

The paid fallback: ~$1 per million input tokens and $5 per million output, halved again
by the Batch API. At ~60 items a day that is roughly $1.20 a month.

Batch is the right shape here. A job that starts at 05:00 with a digest due at 07:00 has
hours of headroom, and the 50% discount is free money for latency nobody experiences.

Switch to this with ENRICH_BACKEND=claude when GLM's malformed-JSON rate stops being
worth the saving.
"""

from __future__ import annotations

import logging
import time

import anthropic

import settings
from enrich.backends.base import EnrichmentBackend, EnrichmentResult, first_text_block
from enrich.prompts import build_user, parse_response

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 400

POLL_INTERVAL_SECONDS = 20
POLL_TIMEOUT_SECONDS = 3600


class ClaudeBackend(EnrichmentBackend):
    name = "claude"

    def __init__(self, model: str | None = None):
        api_key = settings.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Get one from console.anthropic.com, or "
                "use the free backend with ENRICH_BACKEND=glm."
            )
        self.model = model or settings.get("CLAUDE_MODEL") or DEFAULT_MODEL
        self._client = anthropic.Anthropic(api_key=api_key)

    def enrich(self, items: list[dict], system_prompt: str) -> EnrichmentResult:
        result = EnrichmentResult(attempted=len(items))
        if not items:
            return result

        batch = self._client.messages.batches.create(
            requests=[
                {
                    # url_hash as custom_id is what makes results joinable without
                    # keeping any order state -- batch results arrive in ANY order.
                    "custom_id": item["url_hash"],
                    "params": {
                        "model": self.model,
                        "max_tokens": MAX_TOKENS,
                        "system": system_prompt,
                        "messages": [
                            {"role": "user", "content": build_user(item)}
                        ],
                    },
                }
                for item in items
            ]
        )
        log.info("submitted batch %s with %d requests", batch.id, len(items))

        if not self._wait(batch.id):
            log.error("batch %s did not finish in time", batch.id)
            result.failed = len(items)
            return result

        for entry in self._client.messages.batches.results(batch.id):
            if entry.result.type != "succeeded":
                log.warning("batch item %s: %s", entry.custom_id, entry.result.type)
                result.failed += 1
                continue

            payload = parse_response(first_text_block(entry.result.message))
            if payload is None:
                log.warning("unparseable JSON for %s", entry.custom_id)
                result.failed += 1
                continue

            result.rows.append(self._row(entry.custom_id, payload))

        return result

    def _wait(self, batch_id: str) -> bool:
        """Poll until the batch ends. Returns False on timeout."""
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            batch = self._client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
        return False
