#!/usr/bin/env python3
"""Stop hook: the agent cannot finish while the project's tests fail.

Opt-in per repository: the gate does nothing unless the working directory has
`.claude/test_gate.json`. Two forms:

  {"command": "npm test", "max_iterations": 3, "timeout": 600}

  {"rules": [{"match": "api/", "command": "pytest api"},
             {"match": "*.html", "command": "npm run e2e"}],
   "default": "npm test", "max_iterations": 3, "timeout": 600}

With `rules`, only the commands whose `match` hits a changed file (git working
tree, untracked included) run. `match` ending in "/" is a directory prefix, one
containing * ? [ is a glob, anything else is a substring. If git cannot list
changes, `default` runs.

On failure the stop is blocked and the agent sees the test output. After
`max_iterations` consecutive failures in one session the gate gives up, lets
the agent stop and tells the human, so it can never loop forever.
"""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hookio  # noqa: E402

GATE = "test"


def state_path(session_id: str, cwd: str) -> str:
    key = hashlib.sha1(f"{session_id}:{cwd}".encode()).hexdigest()[:16]
    d = os.path.join(tempfile.gettempdir(), "evidence_gates_test_gate")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, key + ".json")


def read_count(path: str) -> int:
    try:
        with open(path) as f:
            return int(json.load(f).get("count", 0))
    except Exception:
        return 0


def write_count(path: str, count: int) -> None:
    try:
        with open(path, "w") as f:
            json.dump({"count": count, "ts": time.time()}, f)
    except Exception:
        pass


def changed_files(cwd: str) -> list[str] | None:
    try:
        proc = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--untracked-files=all"],
                              capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    files = []
    for line in proc.stdout.splitlines():
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        p = p.strip('"')
        if p:
            files.append(p)
    return files


def match_rule(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern)
    return pattern in path


def resolve_commands(cfg: dict, cwd: str) -> list[str]:
    if cfg.get("command"):
        return [cfg["command"]]
    rules = cfg.get("rules")
    if not isinstance(rules, list):
        return []
    default = cfg.get("default")
    files = changed_files(cwd)
    if files is None:
        return [default] if default else []
    cmds: list[str] = []
    for r in rules:
        if not isinstance(r, dict) or not r.get("match") or not r.get("command"):
            continue
        if any(match_rule(r["match"], f) for f in files) and r["command"] not in cmds:
            cmds.append(r["command"])
    if not cmds and default:
        cmds = [default]
    return cmds


def main() -> int:
    if hookio.disabled(GATE):
        return 0
    event = hookio.read_event()
    if event is None:
        return 0
    cwd = event.get("cwd") or os.getcwd()
    session = str(event.get("session_id", "nosession"))
    marker = os.path.join(cwd, ".claude", "test_gate.json")
    if not os.path.isfile(marker):
        return 0
    try:
        with open(marker) as f:
            cfg = json.load(f)
        max_iter = int(cfg.get("max_iterations", 3))
        timeout = int(cfg.get("timeout", 600))
    except Exception:
        return 0  # a broken marker must not stop the work
    commands = resolve_commands(cfg, cwd)
    if not commands:
        return 0

    sp = state_path(session, cwd)
    try:
        lock = open(sp + ".lock", "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # one test run per session+repo at a time
    except OSError:
        hookio.system_message("[test gate] could not take the state lock; tests were not run.")
        return 0

    failed, output = None, ""
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            ok, out = proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            ok, out = False, f"[test gate] `{cmd}` timed out after {timeout}s."
        except Exception:
            write_count(sp, 0)
            return 0  # the command could not even start: do not block on our own failure
        if not ok:
            failed, output = cmd, out
            break

    if failed is None:
        write_count(sp, 0)
        hookio.log_event(GATE, "pass", session)
        return 0

    n = read_count(sp) + 1
    tail = output[-4000:]
    if n >= max_iter:
        # Stay exhausted: later stops rerun the tests but never re-enter the
        # repair loop; only a real pass resets the count.
        write_count(sp, max_iter)
        hookio.log_event(GATE, "give_up", session, failed)
        hookio.system_message(f"[test gate] `{failed}` failed {max_iter} times in a row. Stopping the repair "
                              f"loop; a human needs to look.\nLast output:\n{tail}")
        return 0
    write_count(sp, n)
    hookio.log_event(GATE, "block", session, failed)
    hookio.block_stop(f"`{failed}` is failing (attempt {n}/{max_iter}). Read the output, fix the cause, "
                      f"and finish only when it passes:\n\n{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
