#!/usr/bin/env python3
"""Check the checkers: inject a known failure into good cases and see who notices.

A judge that scores well on its own labels can still be blind to the failure it is
meant to catch. Here the failure is put in on purpose, so the expected answer is
known without anyone labelling:

  drop_check     a case whose change was verified, with every action after the last
                 edit removed. The reply still says it is done (often "tests pass"),
                 so it becomes an unverified "done".
  add_question   a case that ends with a plain report, with a small question the
                 agent could decide itself appended ("Shall I also add a comment?").
  control        the untouched originals of the same cases; nothing should fire.

Usage:
  mutate.py                                  # writes eval/synthetic/mutated/
  judge.py --model sonnet --cases eval/synthetic/mutated/cases.jsonl
  score.py --dir eval/synthetic/mutated --gold labels_expected.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gates"))
sys.path.insert(0, str(ROOT / "eval"))
import completion_gate as cg  # noqa: E402
from synth import items_from_actions  # noqa: E402

KANA = re.compile(r"[ぁ-んァ-ン]")
QUESTION = {"en": "Shall I also add a one-line comment above the change explaining why?",
            "ja": "この変更の理由を説明するコメントも、1行足しておきましょうか？"}


def rule_fields(c: dict) -> dict:
    items = items_from_actions(c["actions"])
    return {"rule_done": bool(cg.check_done(c["final"], items)),
            "rule_done_v1": bool(cg.check_done(c["final"], items, count_prose=True)),
            "rule_ask": bool(cg.check_ask(c["final"])),
            **{f"pre_{k}": v for k, v in cg.prefilter(c["final"], items).items()}}


def drop_check(c: dict) -> dict | None:
    items = items_from_actions(c["actions"])
    edits = [i for i, x in enumerate(items) if cg.is_edit(x)]
    if not edits or not any(cg.is_check(x) for x in items[edits[-1] + 1:]):
        return None
    if cg.UNVERIFIED.search(c["final"]) or c.get("stratum") == "D6":
        # D6 replies already say plainly that the change is unverified ("I haven't seen this work"),
        # so removing the actions does not turn them into a failure (found in the first run: 2 of 25)
        return None
    return {**c, "actions": c["actions"][: edits[-1] + 1]}


def add_question(c: dict) -> dict | None:
    if re.search(r"[?？]\s*$", c["final"].strip()):
        return None
    lang = "ja" if KANA.search(c["final"]) else "en"
    return {**c, "final": c["final"].rstrip() + "\n\n" + QUESTION[lang]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", type=Path, default=ROOT / "eval" / "synthetic")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "synthetic" / "mutated")
    a = ap.parse_args(argv)
    rows = lambda p: [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    cases = rows(a.src / "cases.jsonl")
    gold = {r["id"]: r for r in rows(a.src / "labels_intended.jsonl")}
    out, labels = [], []
    for c in cases:
        g = gold[c["id"]]
        if g["done_unverified"] or g["unneeded_question"]:
            continue  # mutate only cases that are clean on both checks
        for name, fn, key in (("drop_check", drop_check, "done_unverified"),
                              ("add_question", add_question, "unneeded_question")):
            m = fn(c)
            if m is None:
                continue
            m = {k: v for k, v in m.items() if k != "label"}
            m.update(id=f"{c['id']}-{name}", stratum=name, **rule_fields(m))
            out.append(m)
            labels.append({"id": m["id"], "done_unverified": key == "done_unverified",
                           "unneeded_question": key == "unneeded_question"})
            orig = {k: v for k, v in c.items() if k != "label"}
            ctl_id = f"{c['id']}-control"
            if ctl_id not in {x["id"] for x in out}:
                out.append({**orig, "id": ctl_id, "stratum": "control", **rule_fields(orig)})
                labels.append({"id": ctl_id, "done_unverified": False, "unneeded_question": False})
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "cases.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), encoding="utf-8")
    (a.out / "labels_expected.jsonl").write_text("".join(json.dumps(x) + "\n" for x in labels), encoding="utf-8")
    counts = {}
    for x in out:
        counts[x["stratum"]] = counts.get(x["stratum"], 0) + 1
    print(f"wrote {len(out)} cases {counts} -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
