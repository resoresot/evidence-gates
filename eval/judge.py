#!/usr/bin/env python3
"""Run an LLM judge (or the labeler) over cases, through the Claude Code CLI.

The same rubric is given to every model, so differences come from the model,
not from the prompt. The labeler is simply the strongest model with the same
rubric, asked to add a one-line rationale.

Each call is made as lean as the CLI allows: the system prompt is replaced,
no tools, no hooks, no settings, no MCP, no session file. Cost and latency
are the CLI's own measurements of that call.

Usage:
  judge.py --model haiku  [--cases FILE] [--out FILE] [--jobs 8]
  judge.py --model opus --label            # writes the reference labels
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gates"))
from llm_judge import RUBRIC, SCHEMA, cli_command, clean_env, render as render_case  # noqa: E402

PRIVATE = Path.home() / ".local" / "share" / "evidence-gates-eval"

LABEL_SCHEMA = {"type": "object", "properties": {
    "done_unverified": {"type": "boolean"}, "unneeded_question": {"type": "boolean"},
    "rationale": {"type": "string"}},
    "required": ["done_unverified", "unneeded_question", "rationale"]}


def render(case: dict) -> str:
    return render_case(case["user"], case["actions"], case["final"])


def call(model: str, case: dict, label: bool) -> dict:
    schema = LABEL_SCHEMA if label else SCHEMA
    system = RUBRIC + ("\nAdd a one-sentence rationale." if label else "")
    env = clean_env()
    cmd = cli_command(model, render(case), system, schema)
    t0 = time.time()
    for attempt in range(3):
        p = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        try:
            d = json.loads(p.stdout)
            out = d.get("structured_output") or {}
            if "done_unverified" in out:
                return {"id": case["id"], "model": model, **out,
                        "cost_usd": d.get("total_cost_usd", 0), "api_ms": d.get("duration_api_ms"),
                        "wall_ms": int((time.time() - t0) * 1000), "attempts": attempt + 1}
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return {"id": case["id"], "model": model, "error": (p.stderr or p.stdout)[-300:]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--cases", type=Path, default=PRIVATE / "cases.jsonl")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--label", action="store_true", help="reference labels (adds a rationale)")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)
    cases = [json.loads(x) for x in a.cases.read_text(encoding="utf-8").splitlines() if x.strip()]
    out = a.out or a.cases.parent / (f"labels_{a.model}.jsonl" if a.label else f"pred_{a.model}.jsonl")
    done = set()
    if out.exists():  # resume
        done = {json.loads(x)["id"] for x in out.read_text().splitlines() if x.strip() and "error" not in x}
    todo = [c for c in cases if c["id"] not in done]
    print(f"{len(todo)} cases to judge with {a.model} -> {out}", file=sys.stderr)
    with cf.ThreadPoolExecutor(a.jobs) as ex, open(out, "a", encoding="utf-8") as f:
        for r in ex.map(lambda c: call(a.model, c, a.label), todo):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
    rows = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
    errs = sum(1 for r in rows if "error" in r)
    cost = sum(r.get("cost_usd", 0) for r in rows)
    print(f"{len(rows)} results, {errs} errors, total cost ${cost:.4f}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
