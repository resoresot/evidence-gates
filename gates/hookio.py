"""Shared plumbing for the gates: hook I/O, kill switches, and the event log.

Every gate is fail-open: if a gate itself breaks (bad input, unexpected
schema, I/O error), it allows the action. A broken guardrail must never stop
the user's work; it only stops guarding.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def read_event() -> dict | None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def disabled(gate: str) -> bool:
    """EVIDENCE_GATES_DISABLE=all, or a comma list such as `completion,zsh_glob`."""
    raw = os.environ.get("EVIDENCE_GATES_DISABLE", "")
    names = {s.strip() for s in raw.split(",") if s.strip()}
    return "all" in names or gate in names


def log_dir() -> Path:
    base = os.environ.get("EVIDENCE_GATES_LOG_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(base) if base else Path.home() / ".cache" / "evidence-gates"


def log_event(gate: str, decision: str, session: str = "", detail: str = "") -> None:
    """Append one JSON line per decision. No prompt or reply text is stored,
    only what is needed to count how often each gate fires."""
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "gate": gate,
               "decision": decision, "session": session, "detail": detail[:120]}
        with open(d / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def block_stop(reason: str) -> None:
    """Stop / SubagentStop: keep the agent working and show it the reason."""
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def deny_tool(reason: str) -> None:
    """PreToolUse: refuse this tool call and show the agent why."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))


def system_message(text: str) -> None:
    """A note for the human, without blocking."""
    print(json.dumps({"systemMessage": text}, ensure_ascii=False))
