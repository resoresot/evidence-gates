from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "gates"
sys.path.insert(0, str(GATES))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, GATES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(script: str, event: dict, env: dict | None = None, cwd: str | None = None) -> tuple[int, dict | None]:
    """Run a gate as Claude Code would: JSON on stdin, JSON (or nothing) on stdout."""
    log_dir = tempfile.mkdtemp(prefix="eg-log-")
    full_env = {**os.environ, "EVIDENCE_GATES_LOG_DIR": log_dir, **(env or {})}
    full_env.pop("EVIDENCE_GATES_DISABLE", None) if not env or "EVIDENCE_GATES_DISABLE" not in env else None
    p = subprocess.run([sys.executable, str(GATES / script)], input=json.dumps(event, ensure_ascii=False),
                       capture_output=True, text=True, env=full_env, cwd=cwd, timeout=120)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


def write_transcript(rows: list[dict]) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="eg-transcript-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def turn(user: str, *items: dict) -> list[dict]:
    """A user message followed by assistant items; each tool_use gets a tool_result row."""
    rows = [{"message": {"role": "user", "content": user}}]
    for it in items:
        rows.append({"message": {"role": "assistant", "content": [it]}})
        if it.get("type") == "tool_use":
            rows.append({"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}})
    return rows


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}
