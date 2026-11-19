#!/usr/bin/env python3
"""How much does an LLM judge's verdict change when the same case is judged again?

A gate built on a judge blocks or passes the same turn differently from run to run
if the judge is unstable, and a single scoring run hides that. Given k runs of the
same judge over the same cases, this reports, per check:

  stable        share of cases on which all k runs agree
  all-k caught  share of reference positives flagged in every run (pass^k on positives)
  never wrong   share of reference negatives flagged in no run (pass^k on negatives)
  F1 range      lowest and highest F1 over the single runs (unweighted)

Usage:
  stability.py --gold eval/synthetic/labels_intended.jsonl \\
      --runs eval/synthetic/pred_sonnet.jsonl eval/synthetic/repeats/pred_sonnet_r*.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CHECKS = {"done": "done_unverified", "ask": "unneeded_question"}


def load(p: Path) -> dict[str, dict]:
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["id"]: r for r in rows if "error" not in r}


def f1(gold: dict[str, bool], pred: dict[str, bool]) -> float:
    tp = sum(pred[i] and gold[i] for i in pred)
    fp = sum(pred[i] and not gold[i] for i in pred)
    fn = sum(gold[i] and not pred[i] for i in pred)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def report(gold_rows: dict[str, dict], runs: list[dict[str, dict]]) -> list[str]:
    ids = [i for i in gold_rows if all(i in r for r in runs)]
    out = [f"{len(runs)} runs over {len(ids)} cases"]
    for check, key in CHECKS.items():
        gold = {i: bool(gold_rows[i][key]) for i in ids}
        votes = {i: [bool(r[i][key]) for r in runs] for i in ids}
        stable = sum(len(set(v)) == 1 for v in votes.values()) / len(ids)
        pos = [i for i in ids if gold[i]]
        neg = [i for i in ids if not gold[i]]
        caught = sum(all(votes[i]) for i in pos) / len(pos) if pos else float("nan")
        clean = sum(not any(votes[i]) for i in neg) / len(neg) if neg else float("nan")
        f1s = [f1(gold, {i: bool(r[i][key]) for i in ids}) for r in runs]
        out.append(f"  {check}: stable {100 * stable:.0f}% | all-k caught {100 * caught:.0f}% of {len(pos)} positives"
                   f" | never wrong on {100 * clean:.0f}% of {len(neg)} negatives"
                   f" | F1 {100 * min(f1s):.0f}–{100 * max(f1s):.0f}%")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--runs", type=Path, nargs="+", required=True)
    a = ap.parse_args(argv)
    print("\n".join(report(load(a.gold), [load(p) for p in a.runs])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
