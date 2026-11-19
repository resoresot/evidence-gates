#!/usr/bin/env python3
"""Extract turn-end cases from Claude Code transcripts for judging.

A case is what a Stop-time judge sees: the user's request, the actions the
agent took in that turn (compacted), and the final reply. Cases are drawn in
strata so rare situations are not drowned by ordinary turns, and the size of
each stratum in the whole history is kept, so metrics can be re-weighted to the
real mix of turns.

  done_fire     the rule-based done check would block
  done_ok       a completion claim after an edit that the rule lets through
  ask_fire      the rule-based ask check would block
  question      the reply ends with a question the rule lets through
  other         everything else (random sample)

The output contains private conversation text: write it OUTSIDE the
repository (default ~/.local/share/evidence-gates-eval/).

Usage: cases.py [--dir DIR ...] [--out DIR] [--seed 7] [--per done_fire=50,...]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gates"))
import completion_gate as cg  # noqa: E402
from llm_judge import compact_actions  # noqa: E402
from transcript import final_text, user_text  # noqa: E402

DEFAULT_OUT = Path.home() / ".local" / "share" / "evidence-gates-eval"
DEFAULT_PER = {"done_fire": 50, "done_ok": 35, "ask_fire": 25, "question": 40, "other": 45}
ENDS_WITH_QUESTION = re.compile(r"[?？]\s*$")


def turns_of(path: Path, until: str | None = None):
    cur_user, items = None, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("isSidechain"):
                continue
            if until and str(row.get("timestamp", "")) >= until:
                continue  # reproduce the history as it was at extraction time
            t = user_text(row)
            if t is not None:
                if cur_user is not None:
                    yield cur_user, items
                cur_user, items = t, []
                continue
            msg = row.get("message") or {}
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
                items.extend(c for c in msg["content"] if isinstance(c, dict))
    if cur_user is not None:
        yield cur_user, items


def classify(final: str, items: list[dict]) -> str:
    # strata follow the first version of the rule, which the sample was drawn with
    if cg.check_done(final, items, count_prose=True):
        return "done_fire"
    claim = bool(cg.DONE_CLAIM.search(cg.CODE.sub(" ", final)))
    if claim and any(cg.is_edit(c, count_prose=True) for c in items):
        return "done_ok"
    if cg.check_ask(final):
        return "ask_fire"
    if ENDS_WITH_QUESTION.search(cg.last_line(final)):
        return "question"
    return "other"


def build_case(n: int, path: Path, utext: str, items: list[dict], final: str, stratum: str) -> dict:
    actions = compact_actions(items)
    return {
        "id": f"c{n:05d}", "stratum": stratum,
        "user": utext[:1200], "actions": actions, "final": final[-2500:],
        "rule_done": bool(cg.check_done(final, items)), "rule_ask": bool(cg.check_ask(final)),
        "rule_done_v1": bool(cg.check_done(final, items, count_prose=True)),
        **{f"pre_{k}": v for k, v in cg.prefilter(final, items).items()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", action="append", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--until", help="ignore transcript rows at or after this ISO time (reproduce a sample)")
    ap.add_argument("--per", default=",".join(f"{k}={v}" for k, v in DEFAULT_PER.items()))
    a = ap.parse_args(argv)
    if ROOT in a.out.resolve().parents or a.out.resolve() == ROOT:
        print("refusing: cases contain private text and must be written outside the repository", file=sys.stderr)
        return 2
    per = {k: int(v) for k, v in (x.split("=") for x in a.per.split(","))}
    dirs = a.dir or [Path.home() / ".claude" / "projects"]
    pools: dict[str, list] = {k: [] for k in per}
    population = Counter()
    files = sorted(p for d in dirs for p in d.rglob("*.jsonl") if "subagents" not in p.parts)
    for path in files:
        try:
            for utext, items in turns_of(path, a.until):
                final = final_text(items)
                if not final.strip():
                    continue
                s = classify(final, items)
                population[s] += 1
                pools[s].append((path, utext, items, final))
        except Exception as e:
            print(f"skip {path.name}: {type(e).__name__}", file=sys.stderr)
    rng = random.Random(a.seed)
    cases, n = [], 0
    for s, k in per.items():
        chosen = rng.sample(pools[s], min(k, len(pools[s])))
        for path, utext, items, final in chosen:
            n += 1
            cases.append(build_case(n, path, utext, items, final, s))
    rng.shuffle(cases)
    a.out.mkdir(parents=True, exist_ok=True)
    with open(a.out / "cases.jsonl", "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    meta = {"population": dict(population), "sampled": dict(Counter(c["stratum"] for c in cases)),
            "files": len(files), "seed": a.seed, "until": a.until}
    (a.out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
