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


CATEGORY_LABELS = {
    "model_release": "MODEL RELEASE",
    "research": "RESEARCH",
    "tooling": "TOOLING",
    "industry": "INDUSTRY",
    "policy": "POLICY",
    "tutorial": "TUTORIAL",
    "other": "OTHER",
}


def _item_footer(item) -> str:
    """Provenance line: where it came from, how it scored, why it might matter."""
    parts = [item.seen_in or item.source_name, f"{item.final_score:.1f}"]
    if item.max_points:
        parts.append(f"{item.max_points}pts")
    if item.corroborated:
        parts.append(f"{item.corroboration}x corroborated")
    return " · ".join(parts)


def render(digest) -> tuple[str, list[dict]]:
    """Turn a Digest into a Discord header plus one embed per item."""
    date = digest.built_at.strftime("%a %d %b")
    header = f"🛰 **AI Radar** — {date}"

    if digest.is_quiet:
        # Silence is indistinguishable from a crashed job. A quiet day is
        # information; no message at all is anxiety.
        header += f"\n_Quiet day — {len(digest.items)} item(s) cleared the threshold._"

    embeds = []
    for category, items in digest.by_category().items():
        for index, item in enumerate(items):
            title = item.title
            # Label only the first item in each group, so the message reads as
            # sections without repeating the header on every row.
            if index == 0:
                title = f"[{CATEGORY_LABELS.get(category, category.upper())}] {title}"
            description = item.summary
            if item.discussion_url and item.discussion_url != item.url:
                description += f"\n\n{link('discussion', item.discussion_url)}"
            embeds.append(
                build_embed(
                    title=title,
                    url=item.url,
                    description=description,
                    footer=_item_footer(item),
                    category=category,
                )
            )

    return f"{header}\n\n—\n{escape_md(digest.footer())}", embeds


def send_digest(digest, username: str = "AI Radar") -> bool:
    content, embeds = render(digest)
    if not embeds:
        return send(f"{content}\n\n_Nothing cleared the threshold today._",
                    username=username)
    return send_embeds(embeds, content=content, username=username)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the daily digest to Discord.")
    parser.add_argument("--alert", help="send this text as a plain alert")
    args = parser.parse_args()

    if args.alert:
        send(f"⚠️ {args.alert}")
        return

    from deliver.digest import build_digest

    send_digest(build_digest())


if __name__ == "__main__":
    main()
