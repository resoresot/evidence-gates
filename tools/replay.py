#!/usr/bin/env python3
"""Replay the gates over past Claude Code transcripts and measure them.

A gate is only worth its interruptions if the things it stops are really
mistakes. This tool runs the gates' own decision functions over old sessions
(ideally from before the gates were installed, so the agent's behaviour was
not yet shaped by them) and reports aggregate numbers only. No prompt, reply
or command text is printed.

zsh_glob    For every Bash call the gate would have blocked: did the command
            really fail with "no matches found"? (precision). For every
            "no matches found" failure of grep/find/rg: would the gate have
            blocked it? (recall)
completion  For every turn that ends with a claim of completion after an
            edit: was something run after the last edit (verified) or not
            (the gate would block)? Then: how often does the user's next
            message point out that it does not work? For every turn the ask
            check would block: how often was the user's answer a bare
            "yes / go ahead" (the question was not needed)?

Usage:
  replay.py [--dir DIR ...] [--before YYYY-MM-DD] [--after YYYY-MM-DD] [--json]
Default dir: ~/.claude/projects (sub-agent transcripts are skipped).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gates"))
import completion_gate as cg  # noqa: E402
import zsh_glob_gate as zg  # noqa: E402
from transcript import final_text, user_text  # noqa: E402

NOMATCH = re.compile(r"no matches found", re.I)
SEARCH_CMD = re.compile(r"(^|[\s;&|(])(grep|egrep|rg|find|fd)\b")
COMPLAINT = re.compile(
    r"(動かない|動いてない|動いていない|直ってない|直っていない|治ってない|エラー|おかしい|できてない|できていない"
    r"|反映されて(い)?ない|表示されない|変わってない|変わっていない|壊れ|失敗して|違うよ|違います|ダメ|だめ"
    r"|doesn'?t work|not working|still (broken|failing|wrong)|broken|wrong)", re.I)
APPROVAL = re.compile(
    r"^\s*(はい|うん|ok|okay|yes|y|お願い(します)?|おねがい|進めて(ください)?|すすめて|いいよ|いいです|いいね"
    r"|大丈夫(です)?|どうぞ|やって(ください)?|go|go ahead|sure|それで(お願いします)?)[。．.!！、\s]*$", re.I)
def iter_rows(path: Path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and not row.get("isSidechain"):
                yield row


def in_window(ts: str, before: str | None, after: str | None) -> bool:
    day = (ts or "")[:10]
    if not day:
        return False
    return (before is None or day < before) and (after is None or day >= after)


def tool_result_text(c: dict) -> str:
    body = c.get("content")
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)


def replay_file(path: Path, before: str | None, after: str | None, z: Counter, t: Counter) -> None:
    pending: dict[str, bool] = {}           # tool_use_id -> gate would block
    turns: list[tuple[str, list[dict]]] = []  # (user text, assistant items)
    cur_user, cur_items, cur_ok = None, [], False
    for row in iter_rows(path):
        msg = row.get("message") or {}
        content = msg.get("content")
        ut = user_text(row)
        if ut is not None:
            if cur_user is not None and cur_ok:
                turns.append((cur_user, cur_items))
            cur_user, cur_items = ut, []
            cur_ok = in_window(row.get("timestamp", ""), before, after)
            continue
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if msg.get("role") == "assistant":
                cur_items.append(c)
                if c.get("type") == "tool_use" and c.get("name") == "Bash" and cur_ok:
                    cmd = (c.get("input") or {}).get("command")
                    if isinstance(cmd, str):
                        verdict = zg.decide(cmd, "zsh")[0]
                        pending[c.get("id", "")] = (verdict == "block", bool(SEARCH_CMD.search(cmd)))
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                would_block, is_search = pending.pop(c["tool_use_id"])
                text = tool_result_text(c)
                if "hook" in text[:200].lower() and "PreToolUse" in text[:200]:
                    continue  # a live hook already intervened: not a clean sample
                failed = bool(NOMATCH.search(text[:2000]))
                z["bash_calls"] += 1
                if would_block:
                    z["would_block"] += 1
                    z["would_block_and_failed"] += failed
                if failed and is_search:
                    z["nomatch_failures"] += 1
                    z["nomatch_caught"] += would_block
    if cur_user is not None and cur_ok:
        turns.append((cur_user, cur_items))

    for i, (utext, items) in enumerate(turns):
        nxt = turns[i + 1][0] if i + 1 < len(turns) else None
        final = final_text(items)
        if not final.strip():
            continue
        t["turns"] += 1
        claim = bool(cg.DONE_CLAIM.search(cg.CODE.sub(" ", final))) and not cg.UNVERIFIED.search(final)
        edited = any(cg.is_edit(c) for c in items)
        if claim and edited and nxt is not None:
            kind = "unverified" if cg.check_done(final, items) else "verified"
            t[f"done_{kind}"] += 1
            t[f"done_{kind}_complaint"] += bool(COMPLAINT.search(nxt))
        if cg.check_ask(final):
            t["ask_fires"] += 1
            if nxt is not None:
                t["ask_answered"] += 1
                t["ask_bare_approval"] += bool(APPROVAL.match(nxt.strip()[:40]))
        if cg.check_lang(final, utext):
            t["lang_fires"] += 1


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.1f}%)" if b else "-"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", action="append", type=Path)
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    dirs = a.dir or [Path.home() / ".claude" / "projects"]
    files = sorted(p for d in dirs for p in d.rglob("*.jsonl") if "subagents" not in p.parts)
    z, t = Counter(), Counter()
    for p in files:
        try:
            replay_file(p, a.before, a.after, z, t)
        except Exception as e:  # one bad file must not hide the rest
            t["files_failed"] += 1
            print(f"skip {p.name}: {type(e).__name__}", file=sys.stderr)
    t["files"] = len(files)
    if a.json:
        print(json.dumps({"zsh_glob": z, "completion": t}, indent=2))
        return 0
    window = f"{a.after or 'start'} .. {a.before or 'now'}"
    print(f"transcripts: {len(files)} files ({t['files_failed']} could not be read), window {window}, "
          f"turns with a reply: {t['turns']}")
    print("\nzsh_glob gate")
    print(f"  Bash calls replayed: {z['bash_calls']}")
    print(f"  would block: {z['would_block']}; of those really failed with 'no matches found': "
          f"{pct(z['would_block_and_failed'], z['would_block'])}")
    print(f"  search commands that failed with 'no matches found': {z['nomatch_failures']}; "
          f"caught by the gate: {pct(z['nomatch_caught'], z['nomatch_failures'])}")
    print("\ncompletion gate")
    print(f"  'done' after an edit, verified:   user complained next: "
          f"{pct(t['done_verified_complaint'], t['done_verified'])}")
    print(f"  'done' after an edit, unverified: user complained next: "
          f"{pct(t['done_unverified_complaint'], t['done_unverified'])}")
    print(f"  ask check would fire: {t['ask_fires']} of {t['turns']} turns; "
          f"answered with a bare yes/go-ahead: {pct(t['ask_bare_approval'], t['ask_answered'])}")
    print(f"  lang check would fire: {t['lang_fires']} of {t['turns']} turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
