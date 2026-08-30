"""Telegram delivery.

Telegram is the channel of record. One HTTP POST, no auth flow, no domain, no
deliverability problems.

If TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset the message is printed to stdout
instead of sent, so the whole pipeline is runnable before any credentials exist.
"""

from __future__ import annotations

import argparse
import os

import httpx

API_BASE = "https://api.telegram.org"

# Telegram rejects a message outright if any of these appear unescaped in MarkdownV2.
# A single unescaped '.' or '-' in a post title is enough to silently drop the digest.
MDV2_RESERVED = r"_*[]()~`>#+-=|{}.!\\"

# Hard API limit. Longer messages are rejected, not truncated.
MAX_MESSAGE_CHARS = 4096


def escape_md(text: str) -> str:
    """Escape text for MarkdownV2."""
    return "".join(f"\\{ch}" if ch in MDV2_RESERVED else ch for ch in str(text))


def link(text: str, url: str) -> str:
    """A MarkdownV2 inline link. Label and URL have different escaping rules."""
    safe_url = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{escape_md(text)}]({safe_url})"


def chunk(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so escape sequences are never cut in half."""
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


def send(text: str, *, markdown: bool = True) -> bool:
    """Send `text`, splitting if needed. Returns True if it actually went to Telegram."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset — printing instead:\n")
        print(text)
        return False

    for part in chunk(text):
        payload = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
        if markdown:
            payload["parse_mode"] = "MarkdownV2"
        response = httpx.post(
            f"{API_BASE}/bot{token}/sendMessage", json=payload, timeout=30
        )
        # Surface Telegram's reason rather than a bare 400 — it names the offending
        # character offset, which is how you find a missed escape.
        if response.status_code != 200:
            raise RuntimeError(f"telegram send failed: {response.text}")
    return True


def _sample() -> str:
    """Phase 0 walking skeleton: 5 titles straight out of DuckDB."""
    import duckdb

    rows = duckdb.connect("ai_radar.duckdb", read_only=True).execute(
        """
        select title, url, source_name
        from raw.items
        order by published_at desc
        limit 5
        """
    ).fetchall()

    lines = ["🛰 *AI Radar* — walking skeleton", ""]
    lines += [
        f"▸ {link(title, url)}\n  ↳ {escape_md(source)}"
        for title, url, source in rows
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Telegram message.")
    parser.add_argument("--alert", help="send this text as a plain-text alert")
    args = parser.parse_args()

    if args.alert:
        send(f"⚠️ {args.alert}", markdown=False)
    else:
        send(_sample())


if __name__ == "__main__":
    main()
