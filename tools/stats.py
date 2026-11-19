#!/usr/bin/env python3
"""Summarize the gates' own decision log (events.jsonl) by week.

Every gate writes one line per decision (block / pass / unsure / give_up,
plus lesson injections). This turns that log into fire rates per gate and
per check, so a gate that silently stopped firing, or started firing on
everything, shows up in the weekly numbers.

Usage: stats.py [--log FILE] [--weeks 8]
Default log: $EVIDENCE_GATES_LOG_DIR/events.jsonl, else ~/.cache/evidence-gates/events.jsonl
(inside a plugin the gates write to the plugin data folder; pass --log for that).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


def default_log() -> Path:
    base = os.environ.get("EVIDENCE_GATES_LOG_DIR")
    return (Path(base) if base else Path.home() / ".cache" / "evidence-gates") / "events.jsonl"


def week_of(ts: str) -> str:
    d = dt.date.fromisoformat(ts[:10])
    monday = d - dt.timedelta(days=d.weekday())
    return monday.isoformat()


def summarize(rows: list[dict], weeks: int) -> list[str]:
    by_week: dict[str, Counter] = defaultdict(Counter)
    checks: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        try:
            w = week_of(r["ts"])
        except Exception:
            continue
        gate, decision = r.get("gate", "?"), r.get("decision", "?")
        by_week[w][(gate, decision)] += 1
        if gate == "completion" and decision == "block":
            for c in str(r.get("detail", "")).split(","):
                if c:
                    checks[w][c] += 1
    out = ["| week | gate | blocks | passes | fire rate | other |", "|---|---|---|---|---|---|"]
    for w in sorted(by_week)[-weeks:]:
        counts = by_week[w]
        for gate in sorted({g for g, _ in counts}):
            b, p = counts[(gate, "block")], counts[(gate, "pass")]
            other = {d: n for (g, d), n in counts.items() if g == gate and d not in ("block", "pass")}
            rate = f"{100 * b / (b + p):.1f}%" if b + p else "-"
            extra = ", ".join(f"{d} {n}" for d, n in sorted(other.items()))
            if gate == "completion" and checks[w]:
                extra = ", ".join(filter(None, [extra, " ".join(f"{c}:{n}" for c, n in sorted(checks[w].items()))]))
            out.append(f"| {w} | {gate} | {b} | {p} | {rate} | {extra} |")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", type=Path, default=default_log())
    ap.add_argument("--weeks", type=int, default=8)
    a = ap.parse_args(argv)
    if not a.log.is_file():
        print(f"no log at {a.log}", file=sys.stderr)
        return 1
    rows = []
    for line in a.log.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    print("\n".join(summarize(rows, a.weeks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
