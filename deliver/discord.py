"""Discord delivery — the primary channel.

A webhook is a plain URL that turns an HTTP POST into a message. No bot to register,
no approval flow, no token refresh. Create a private server for yourself, then
Channel → Integrations → Webhooks → Copy Webhook URL.

Discord's markdown is far friendlier than Telegram's MarkdownV2: only a handful of
characters are special, and an unescaped one degrades formatting rather than causing the
API to reject the entire message.

If DISCORD_WEBHOOK_URL is unset the message is printed to stdout instead of sent, so the
pipeline runs fully before any credentials exist.
"""

from __future__ import annotations

import argparse
import json
import re
import time

import httpx

import settings

# Discord's per-message content limit. Telegram's is 4096 — this one is half that.
MAX_CONTENT_CHARS = 2000

# Embed limits, from the Discord API docs.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_DESCRIPTION = 4096

# Characters that trigger Discord markdown. Escaping is cosmetic here, not a hard
# API requirement, but an unescaped '_' in a source name still renders as italics.
_MD_SPECIAL = re.compile(r"([\\*_~`|])")

# Sidebar colour per category, so the digest is skimmable at a glance.
CATEGORY_COLORS = {
    "model_release": 0xE8590C,  # orange — the things you most want to see
    "research": 0x1971C2,  # blue
    "tooling": 0x2F9E44,  # green
    "industry": 0x868E96,  # grey
    "policy": 0x862E9C,  # purple
    "tutorial": 0x0C8599,  # teal
    "other": 0x495057,
}
DEFAULT_COLOR = CATEGORY_COLORS["other"]


def escape_md(text) -> str:
    """Escape Discord markdown in a plain-text run."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def link(text: str, url: str) -> str:
    """A masked link. Valid inside embed descriptions; plain content renders it literally.

    A ']' in the label or a ')' in the URL would terminate the link early, so the label
    is escaped and the URL percent-encodes its parens.
    """
    label = escape_md(text).replace("[", "\\[").replace("]", "\\]")
    safe_url = url.replace("(", "%28").replace(")", "%29")
    return f"[{label}]({safe_url})"


def chunk(text: str, limit: int = MAX_CONTENT_CHARS) -> list[str]:
    """Split on line boundaries so an escape sequence is never cut in half."""
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _post(webhook_url: str, payload: dict) -> None:
    """POST once, honouring a 429 by waiting the interval Discord asks for."""
    for attempt in range(2):
        response = httpx.post(webhook_url, json=payload, timeout=30)
        if response.status_code == 429 and attempt == 0:
            retry_after = response.json().get("retry_after", 1)
            time.sleep(float(retry_after))
            continue
        # 204 No Content is the success case for a webhook post.
        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"discord send failed ({response.status_code}): {response.text}"
            )
        return


def send(text: str, *, username: str = "AI Radar") -> bool:
    """Send plain text, splitting if needed. True if it actually reached Discord."""
    webhook_url = settings.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[discord] DISCORD_WEBHOOK_URL unset — printing instead:\n")
        print(text)
        return False

    for part in chunk(text):
        _post(webhook_url, {"username": username, "content": part})
    return True


def send_embeds(
    embeds: list[dict], *, content: str = "", username: str = "AI Radar"
) -> bool:
    """Send rich embeds, batched to Discord's 10-per-message limit."""
    webhook_url = settings.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[discord] DISCORD_WEBHOOK_URL unset — printing instead:\n")
        if content:
            print(content)
        print(json.dumps(embeds, indent=2)[:4000])
        return False

    batches = [
        embeds[i : i + MAX_EMBEDS_PER_MESSAGE]
        for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE)
    ]
    for index, batch in enumerate(batches):
        payload = {"username": username, "embeds": batch}
        if index == 0 and content:
            payload["content"] = content[:MAX_CONTENT_CHARS]
        _post(webhook_url, payload)
    return True


def build_embed(
    title: str, url: str, description: str, footer: str, category: str = "other"
) -> dict:
    """One digest item as an embed."""
    return {
        "title": title[:256],
        "url": url,
        "description": description[:MAX_EMBED_DESCRIPTION],
        "color": CATEGORY_COLORS.get(category, DEFAULT_COLOR),
        "footer": {"text": footer[:2048]},
    }


def _sample() -> tuple[str, list[dict]]:
    """Phase 0 walking skeleton: 5 most recent items straight out of DuckDB."""
    import duckdb

    rows = duckdb.connect("ai_radar.duckdb", read_only=True).execute(
        """
        select title, url, summary_raw, source_name
        from raw.items
        order by published_at desc
        limit 5
        """
    ).fetchall()

    embeds = [
        build_embed(
            title=title,
            url=url,
            description=(summary or "")[:300],
            footer=source,
        )
        for title, url, summary, source in rows
    ]
    return "🛰 **AI Radar** — walking skeleton", embeds


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Discord message.")
    parser.add_argument("--alert", help="send this text as a plain alert")
    args = parser.parse_args()

    if args.alert:
        send(f"⚠️ {args.alert}")
    else:
        content, embeds = _sample()
        send_embeds(embeds, content=content)


if __name__ == "__main__":
    main()
