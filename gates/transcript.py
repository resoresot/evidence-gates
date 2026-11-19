"""Read Claude Code transcripts (JSONL).

The gates (live) and tools/replay.py (offline) both use this module, so a
"turn" means the same thing when a gate decides and when it is measured.
"""
from __future__ import annotations

import json
import re

# Text that arrives as a user message but was not typed by the user.
SYNTHETIC_USER = re.compile(
    r"^\s*(<|Stop hook feedback|Caveat:|\[Request interrupted|This session is being continued)")


def user_text(row: dict) -> str | None:
    """Text of a message the user really typed, or None (tool results, hook
    feedback, injected reminders, sub-agent rows)."""
    if row.get("isSidechain") or row.get("isMeta"):
        return None
    msg = row.get("message") or {}
    if msg.get("role") != "user":
        return None
    c = msg.get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str) or not c.strip() or SYNTHETIC_USER.match(c):
        return None
    return c


def final_text(items: list[dict]) -> str:
    """The reply the user sees at the end of a turn: text after the last tool call."""
    tail: list[str] = []
    for c in reversed(items):
        if c.get("type") == "tool_use":
            break
        if c.get("type") == "text":
            tail.insert(0, c.get("text", ""))
    return "\n".join(tail)


def load_turn(path: str | None, last_message: str | None = None) -> tuple[str, list[dict], str]:
    """Return (final reply text, assistant content items of this turn, user's last message).

    A turn starts at the user's last real message; hook feedback and tool
    results inside it do not start a new turn.
    """
    rows: list[dict] = []
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except Exception:
            rows = []
    items: list[dict] = []
    utext = ""
    for row in reversed(rows):
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        t = user_text(row)
        if t is not None:
            utext = t
            break
        msg = row.get("message") or {}
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            items[:0] = [c for c in msg["content"] if isinstance(c, dict)]
    final = last_message if isinstance(last_message, str) else final_text(items)
    return final or "", items, utext
