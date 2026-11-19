#!/usr/bin/env python3
"""The lessons loop: record a mistake once, count every time it happens again,
and promote the ones that keep recurring into gates.

Written guidance alone does not stop an agent from repeating a mistake. This
module makes that measurable. A lesson has three parts:

    when  the situation in which it applies
    do    what to do (phrased positively)
    why   the incident that taught it (date, numbers)

Each time the same mistake happens again, `recur` adds a dated entry. `report`
ranks lessons by recurrence; a lesson that keeps recurring while it is being
shown to the agent is a candidate for a hook that enforces it (this repo's
gates started that way).

Storage: one JSON line per lesson, in
  - $EVIDENCE_GATES_LESSONS, or ~/.config/evidence-gates/lessons.jsonl (global)
  - .claude/lessons.jsonl in the project (project-specific, optional)

Usage:
  lessons.py add --when "..." --do "..." --why "..." [--tags a,b] [--project]
  lessons.py recur L003 [--note "..."]
  lessons.py promote L003 --gate completion_gate.done
  lessons.py list | show L003 | report [--min 2]
  lessons.py hook        # UserPromptSubmit: inject up to 3 relevant lessons
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hookio  # noqa: E402

GATE = "lessons"
MAX_INJECT = 3
MIN_SCORE = 2


def global_store() -> Path:
    env = os.environ.get("EVIDENCE_GATES_LESSONS")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "evidence-gates" / "lessons.jsonl"


def project_store(cwd: str | None = None) -> Path:
    return Path(cwd or os.getcwd()) / ".claude" / "lessons.jsonl"


def load(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def save(path: Path, lessons: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lessons), encoding="utf-8")
    tmp.replace(path)


def all_lessons(cwd: str | None = None) -> list[tuple[Path, dict]]:
    out = [(global_store(), x) for x in load(global_store())]
    proj = project_store(cwd)
    if proj.resolve() != global_store().resolve():
        out += [(proj, x) for x in load(proj)]
    return out


def next_id(cwd: str | None = None) -> str:
    nums = [int(m.group(1)) for _, x in all_lessons(cwd) if (m := re.match(r"L(\d+)$", str(x.get("id", ""))))]
    return f"L{max(nums, default=0) + 1:03d}"


def find(lesson_id: str, cwd: str | None = None) -> tuple[Path, list[dict], dict]:
    for path in (global_store(), project_store(cwd)):
        items = load(path)
        for x in items:
            if x.get("id") == lesson_id:
                return path, items, x
    raise SystemExit(f"no lesson {lesson_id}")


# ---------------------------------------------------------------- relevance
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}")
_JA = re.compile(r"[぀-ヿ㐀-鿿]+")
_HIRAGANA_ONLY = re.compile(r"^[぀-ゟ]+$")
STOP = {"the", "and", "for", "with", "when", "that", "this", "from", "into", "you", "are", "not"}


def tokens(text: str) -> set[str]:
    """English words, plus character bigrams for Japanese (no tokenizer needed)."""
    out = {w.lower() for w in _WORD.findall(text or "")} - STOP
    for run in _JA.findall(text or ""):
        # bigrams made only of hiragana are particles and endings (する, とき),
        # which match almost any Japanese request
        out |= {b for b in (run[i:i + 2] for i in range(len(run) - 1)) if not _HIRAGANA_ONLY.match(b)}
    return out


def score(prompt_tokens: set[str], lesson: dict) -> int:
    key = " ".join([lesson.get("when", ""), " ".join(lesson.get("tags", []))])
    return len(prompt_tokens & tokens(key))


def relevant(prompt: str, cwd: str | None = None) -> list[dict]:
    pt = tokens(prompt)
    scored = [(score(pt, x), x) for _, x in all_lessons(cwd) if not x.get("promoted_to")]
    scored = [(s, x) for s, x in scored if s >= MIN_SCORE]
    scored.sort(key=lambda sx: (-sx[0], -len(sx[1].get("recurrences", []))))
    return [x for _, x in scored[:MAX_INJECT]]


def render(lesson: dict) -> str:
    n = len(lesson.get("recurrences", []))
    again = f" (recurred {n}x)" if n else ""
    return f"- {lesson['id']}{again} when: {lesson['when']} -> do: {lesson['do']}"


# ---------------------------------------------------------------- commands
def today() -> str:
    return dt.date.today().isoformat()


def cmd_add(a) -> None:
    path = project_store() if a.project else global_store()
    items = load(path)
    lesson = {"id": next_id(), "when": a.when, "do": a.do, "why": a.why,
              "tags": [t.strip() for t in (a.tags or "").split(",") if t.strip()],
              "created": today(), "recurrences": [], "promoted_to": None}
    items.append(lesson)
    save(path, items)
    print(lesson["id"])


def cmd_recur(a) -> None:
    path, items, x = find(a.id)
    x.setdefault("recurrences", []).append({"at": a.at or today(), "note": a.note or ""})
    save(path, items)
    print(f"{x['id']} recurred {len(x['recurrences'])}x")


def cmd_promote(a) -> None:
    path, items, x = find(a.id)
    x["promoted_to"] = a.gate
    x["promoted_at"] = today()
    save(path, items)
    print(f"{x['id']} -> {a.gate}")


def cmd_list(_a) -> None:
    for _, x in all_lessons():
        print(render(x) + (f"  [gate: {x['promoted_to']}]" if x.get("promoted_to") else ""))


def cmd_show(a) -> None:
    print(json.dumps(find(a.id)[2], ensure_ascii=False, indent=2))


def cmd_report(a) -> None:
    rows = [x for _, x in all_lessons()]
    total = len(rows)
    recurring = sorted((x for x in rows if len(x.get("recurrences", [])) >= a.min),
                       key=lambda x: -len(x.get("recurrences", [])))
    print(f"lessons: {total}, recurred at least {a.min}x: {len(recurring)}")
    print("| id | recurrences | last | status | when |")
    print("|---|---|---|---|---|")
    for x in recurring:
        rec = x.get("recurrences", [])
        status = f"gate: {x['promoted_to']}" if x.get("promoted_to") else "candidate for a gate"
        print(f"| {x['id']} | {len(rec)} | {rec[-1]['at'] if rec else '-'} | {status} | {x['when'][:60]} |")


def cmd_hook(_a) -> int:
    """UserPromptSubmit: show the agent the lessons that match this request."""
    if hookio.disabled(GATE):
        return 0
    event = hookio.read_event()
    if event is None:
        return 0
    try:
        hits = relevant(str(event.get("prompt", "")), event.get("cwd"))
    except Exception:
        return 0
    if not hits:
        return 0
    hookio.log_event(GATE, "inject", str(event.get("session_id", "")), ",".join(x["id"] for x in hits))
    text = ("Lessons recorded from earlier mistakes that may apply here. Follow them; if one turns out to be "
            "wrong for this case, say so.\n" + "\n".join(render(x) for x in hits))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": text}}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lessons.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--when", required=True)
    s.add_argument("--do", required=True)
    s.add_argument("--why", required=True)
    s.add_argument("--tags")
    s.add_argument("--project", action="store_true", help="store in .claude/lessons.jsonl of this project")
    s = sub.add_parser("recur")
    s.add_argument("id")
    s.add_argument("--note")
    s.add_argument("--at", help="date of the incident (default: today)")
    s = sub.add_parser("promote")
    s.add_argument("id")
    s.add_argument("--gate", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("id")
    s = sub.add_parser("report")
    s.add_argument("--min", type=int, default=2)
    sub.add_parser("hook")
    a = p.parse_args(argv)
    if a.cmd == "hook":
        return cmd_hook(a)
    {"add": cmd_add, "recur": cmd_recur, "promote": cmd_promote, "list": cmd_list,
     "show": cmd_show, "report": cmd_report}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
