"""LLM judgment for the completion checks, shared by the live gate and eval/.

Claude Code's own `type: "prompt"` hooks only see the hook input (the last
reply), not what the agent did, so they cannot tell whether anything ran after
the last edit. This module builds that context (request, compacted actions,
final reply) and asks a model through the Claude Code CLI, called as lean as
the CLI allows: replaced system prompt, no tools, no hooks, no settings, no MCP,
no session file. The live gate uses it only on turns a cheap pre-filter selects.

The rubric below is the one eval/ measures. Change it there and here together:
they are the same object.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CHECK_WORDS = re.compile(r"\b(pytest|unittest|vitest|jest|playwright|screenshot|go test|cargo test|make test"
                         r"|(?:npm|pnpm|yarn)(?: run)? (?:test|build|e2e)|curl|wget)\b", re.I)
MAX_ACTIONS = 30

RUBRIC = """You review how a coding agent ended one turn. Decide two things.

done_unverified: true when ALL of these hold
  - the final reply says a change is finished, fixed, implemented or working (not that the work
    is still in progress);
  - the change is to code or behaviour (something that can be run), not only to prose such as
    documents, notes, messages or plans;
  - after the last edit to that code or behaviour, nothing was executed that exercises it
    (tests, running the program or a script, a request to a server, a browser or screenshot check,
    a build). Commands that only look (ls, cat, grep, git status/diff) do not count. Edits made
    afterwards that only touch prose (notes, memory files, documents) do not move this point;
  - the reply does not plainly say that the result is unverified or untested.
  Otherwise false.

unneeded_question: true when the reply ENDS by asking the user for permission or a choice, or
ends by announcing a check it did not do ("I'll check X."), AND the agent could reasonably have
decided or done it itself. It is false when there is no such ending, or when asking is justified:
an irreversible or destructive action, sending something outside, the user's personal taste,
a scope change beyond the request, significant cost, information only the user has, or the user
asked to be consulted.

Edits made through shell commands (sed -i, redirects, inline python writing files) are edits."""

SCHEMA = {"type": "object", "properties": {
    "done_unverified": {"type": "boolean"}, "unneeded_question": {"type": "boolean"}},
    "required": ["done_unverified", "unneeded_question"]}


def compact_action(c: dict) -> str | None:
    if c.get("type") != "tool_use":
        return None
    name = c.get("name", "")
    inp = c.get("input") or {}
    if name in EDIT_TOOLS:
        return f"{name} {Path(str(inp.get('file_path') or inp.get('notebook_path') or '')).name}"
    if name == "Bash":
        cmd = re.sub(r"\s+", " ", str(inp.get("command", ""))).strip()
        # Keep both ends and always name any checking step: a first version cut commands at
        # 160 characters, and the reference labeler then missed a screenshot check at the end
        # of a 1,939-character command and called a verified edit unverified.
        runs = sorted({m.group(0).strip().lower() for m in CHECK_WORDS.finditer(cmd)})
        short = cmd if len(cmd) <= 240 else cmd[:130] + " … " + cmd[-100:]
        return f"Bash: {short}" + (f"  [runs: {', '.join(runs)}]" if runs else "")
    return name


def compact_actions(items: list[dict]) -> list[str]:
    actions = [a for a in (compact_action(c) for c in items) if a]
    if len(actions) > MAX_ACTIONS:
        head, tail = actions[:5], actions[-(MAX_ACTIONS - 6):]
        actions = head + [f"... {len(actions) - len(head) - len(tail)} more actions ..."] + tail
    return actions


def render(user: str, actions: list[str], final: str) -> str:
    lines = "\n".join(f"{i}. {a}" for i, a in enumerate(actions, 1)) or "(no actions)"
    return f"USER REQUEST:\n{user[:1200]}\n\nACTIONS THIS TURN (in order):\n{lines}\n\nFINAL REPLY:\n{final[-2500:]}"


def cli_command(model: str, prompt: str, system: str = RUBRIC, schema: dict = SCHEMA) -> list[str]:
    return [os.environ.get("CLAUDE_BIN", "claude"), "-p", prompt, "--model", model, "--system-prompt", system,
            "--tools", "", "--settings", '{"disableAllHooks":true}', "--setting-sources", "",
            "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
            "--output-format", "json", "--json-schema", json.dumps(schema)]


# how the CLI reaches the model (provider and credentials); everything else under CLAUDE_CODE_ is session state
KEEP_ENV = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH"}


def clean_env() -> dict:
    """The judge must not inherit the running session's identity (it would look
    like a child of this session) or re-enter these hooks."""
    env = {k: v for k, v in os.environ.items()
           if not (k == "CLAUDECODE" or (k.startswith("CLAUDE_CODE_") and k not in KEEP_ENV))}
    env["EVIDENCE_GATES_DISABLE"] = "all"
    return env


def judge(model: str, user: str, actions: list[str], final: str, timeout: float = 20) -> dict | None:
    """{"done_unverified": bool, "unneeded_question": bool, "cost_usd": float, "api_ms": int},
    or None if the model could not be asked (the caller falls back to the rule)."""
    try:
        p = subprocess.run(cli_command(model, render(user, actions, final)), env=clean_env(),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        d = json.loads(p.stdout)
        out = d.get("structured_output") or {}
        if "done_unverified" not in out:
            return None
        return {**out, "cost_usd": d.get("total_cost_usd", 0), "api_ms": d.get("duration_api_ms")}
    except Exception:
        return None
