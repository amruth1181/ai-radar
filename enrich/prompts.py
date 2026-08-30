"""The triage prompt and its JSON contract.

The model's job is triage, not summarization. It scores ~60 candidates against one
specific reader's profile so that reader opens ten. The summary is a side effect.

One item per request, deliberately. Batching several items into one prompt lets them
contaminate each other — a mediocre paper next to three strong ones drifts upward, and
scores stop being comparable across runs.
"""

from __future__ import annotations

import json
import re

CATEGORIES = (
    "model_release",
    "research",
    "tooling",
    "industry",
    "policy",
    "tutorial",
    "other",
)

SYSTEM = """You triage AI/ML news for one specific reader. You output only JSON.

READER PROFILE:
{profile}

HIGH INTEREST:
{high_interest}

LOW INTEREST:
{low_interest}

Score relevance 0-10 for THIS reader specifically, not general importance.
A major funding round is important news but scores 2 for this reader.
A small dbt-adjacent tooling release scores 8.

Be decisive. Most items are not interesting; use the low end of the range freely.
Reserve 8-10 for things this reader would genuinely stop and read today.

Output exactly this JSON, no markdown fences, no preamble:
{{
  "summary": "<2 sentences, concrete, no hype words, no 'revolutionary'>",
  "category": "<{categories}>",
  "entities": ["<org/model/library names, max 5>"],
  "relevance_score": <0-10 integer>,
  "reason": "<max 12 words on why this score>"
}}"""

USER = """TITLE: {title}
SOURCE: {source_name}
PUBLISHED: {published_at}
CONTENT: {summary_raw}"""

# Models sometimes wrap JSON in fences despite being told not to.
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Last resort: pull the outermost JSON object out of surrounding prose.
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

MAX_CONTENT_CHARS = 2000


def build_system(profile_cfg: dict) -> str:
    """Render the system prompt from profile.yaml.

    Kept stable across items in a run so the prefix stays cacheable.
    """
    return SYSTEM.format(
        profile=profile_cfg.get("profile", "").strip(),
        high_interest="\n".join(f"- {x}" for x in profile_cfg.get("high_interest", [])),
        low_interest="\n".join(f"- {x}" for x in profile_cfg.get("low_interest", [])),
        categories="|".join(CATEGORIES),
    )


def build_user(item: dict) -> str:
    return USER.format(
        title=item["title"],
        source_name=item["source_name"],
        published_at=item["published_at"],
        summary_raw=(item.get("summary_raw") or "")[:MAX_CONTENT_CHARS],
    )


def parse_response(text: str) -> dict | None:
    """Parse and validate one model response. Returns None if unusable.

    Never raises: a malformed response for one item must not abort the batch. The
    caller counts the failures, and that rate is the number that decides whether a
    backend is good enough.
    """
    if not text:
        return None

    candidate = _FENCE.sub("", text.strip())
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        match = _OBJECT.search(candidate)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(payload, dict):
        return None

    score = payload.get("relevance_score")
    try:
        score = int(score)
    except (TypeError, ValueError):
        return None
    # Clamp rather than reject: an out-of-range score is a formatting slip, not a
    # reason to throw away an otherwise good summary. The dbt accepted_range test
    # would fail the whole build on an 11.
    score = max(0, min(10, score))

    category = payload.get("category")
    if category not in CATEGORIES:
        category = "other"

    entities = payload.get("entities")
    if not isinstance(entities, list):
        entities = []
    entities = [str(e) for e in entities[:5]]

    return {
        "summary": str(payload.get("summary") or "").strip()[:1000],
        "category": category,
        "entities": entities,
        "relevance_score": score,
        "reason": str(payload.get("reason") or "").strip()[:200],
    }
