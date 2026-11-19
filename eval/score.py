#!/usr/bin/env python3
"""Score judges against reference labels: quality, cost and latency.

Judges compared:
  rule            the regex checks in gates/completion_gate.py (free, instant)
  llm:<model>     an LLM reads every turn end
  hybrid:<model>  the rule decides first; the LLM is asked only when the rule
                  would block, and the block stands only if the LLM agrees

Cases were drawn in strata (see cases.py). Metrics are re-weighted by each
stratum's share of all turns, and 95% intervals come from a stratified
bootstrap. Cost and latency are per 1,000 turn ends, as a live gate pays them.

Usage: score.py [--dir DIR] [--gold labels_opus.jsonl] [--human human_labels.json] [--models haiku,sonnet]
Output contains aggregates only (safe to publish).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

PRIVATE = Path.home() / ".local" / "share" / "evidence-gates-eval"
CHECKS = {"done": ("rule_done", "done_unverified"), "ask": ("rule_ask", "unneeded_question")}
# Cheap pre-filters: turns where the check can apply at all (a completion claim after an
# edit; a reply ending in a question or an announced check). The LLM reads only these.
PREFILTER = {"done": {"done_fire", "done_ok"}, "ask": {"ask_fire", "question"}}


def load_jsonl(p: Path) -> dict[str, dict]:
    if not p.exists():
        return {}
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["id"]: r for r in rows if "error" not in r}


def prefiltered(c: dict, check: str) -> bool:
    """The live gate's own pre-filter verdict when recorded, else the stratum."""
    return c[f"pre_{check}"] if f"pre_{check}" in c else c["stratum"] in PREFILTER[check]


def predictions(cases, preds: dict[str, dict[str, dict]], check: str) -> dict[str, dict[str, bool]]:
    rule_key, llm_key = CHECKS[check]
    out = {}
    if check == "done" and all("rule_done_v1" in c for c in cases):
        out["rule v1 (prose edits count)"] = {c["id"]: c["rule_done_v1"] for c in cases}
    out["rule"] = {c["id"]: c[rule_key] for c in cases}
    for model, p in preds.items():
        out[f"llm:{model}"] = {c["id"]: bool(p[c["id"]][llm_key]) for c in cases if c["id"] in p}
        out[f"hybrid:{model}"] = {c["id"]: c[rule_key] and bool(p[c["id"]][llm_key]) for c in cases if c["id"] in p}
        out[f"prefilter+{model}"] = {c["id"]: prefiltered(c, check) and bool(p[c["id"]][llm_key])
                                     for c in cases if c["id"] in p}
    return out


def weighted_prf(cases, weights, gold, pred):
    tp = fp = fn = 0.0
    for c in cases:
        i = c["id"]
        if i not in pred or i not in gold:
            continue
        w = weights[c["stratum"]]
        tp += w * (pred[i] and gold[i])
        fp += w * (pred[i] and not gold[i])
        fn += w * (not pred[i] and gold[i])
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if p == p and r == r and p + r else float("nan")
    return p, r, f


def bootstrap_f1(cases, weights, gold, pred, reps=1000, seed=1):
    rng = random.Random(seed)
    by = {}
    for c in cases:
        by.setdefault(c["stratum"], []).append(c)
    vals = []
    for _ in range(reps):
        sample = [rng.choice(v) for v in by.values() for _ in v]
        f = weighted_prf(sample, weights, gold, pred)[2]
        if f == f:
            vals.append(f)
    if len(vals) < reps * 0.5:
        return float("nan"), float("nan")
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def kappa(a: list[bool], b: list[bool]) -> float:
    n = len(a)
    if not n:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def fmt(x, pct=True):
    if x != x:
        return "-"
    return f"{100 * x:.0f}%" if pct else f"{x:.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", type=Path, default=PRIVATE)
    ap.add_argument("--gold", default="labels_opus.jsonl")
    ap.add_argument("--human", default="human_labels.json")
    ap.add_argument("--second", default="labels_second.jsonl",
                    help="an independent second labeler (another vendor's model, same rubric)")
    ap.add_argument("--models", default="haiku,sonnet")
    a = ap.parse_args(argv)
    cases = [json.loads(x) for x in (a.dir / "cases.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    meta_path = a.dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    pop = meta.get("population") or {}
    sampled = {}
    for c in cases:
        sampled[c["stratum"]] = sampled.get(c["stratum"], 0) + 1
    weights = {s: (pop.get(s, n) / n) for s, n in sampled.items()}
    gold_rows = load_jsonl(a.dir / a.gold)
    preds = {m: load_jsonl(a.dir / f"pred_{m}.jsonl") for m in a.models.split(",")}
    total_turns = sum(pop.values()) if pop else len(cases)
    out = [f"cases: {len(cases)} (strata {json.dumps(sampled)}); turns in history: {total_turns}; "
           f"reference labels: {len(gold_rows)}"]

    human_path = a.dir / a.human
    if human_path.exists():
        human = json.loads(human_path.read_text(encoding="utf-8"))
        out.append("\nreference labeler vs a human, on the human-labelled subset:")
        for check, (_, key) in CHECKS.items():
            ids = [i for i in human if i in gold_rows and key in human[i]]
            ha = [bool(human[i][key]) for i in ids]
            ga = [bool(gold_rows[i][key]) for i in ids]
            agree = sum(x == y for x, y in zip(ha, ga))
            out.append(f"  {check}: agreement {agree}/{len(ids)}, Cohen's kappa {fmt(kappa(ha, ga), False)}")

    second_path = a.dir / a.second
    if second_path.exists():
        second = {i: r for i, r in load_jsonl(second_path).items() if "error" not in r}
        out.append(f"\nreference labeler vs a second, independent labeler ({a.second}):")
        for check, (_, key) in CHECKS.items():
            ids = [i for i in second if i in gold_rows]
            sa = [bool(second[i][key]) for i in ids]
            ga = [bool(gold_rows[i][key]) for i in ids]
            agree = sum(x == y for x, y in zip(sa, ga))
            out.append(f"  {check}: agreement {agree}/{len(ids)}, Cohen's kappa {fmt(kappa(sa, ga), False)}")

    # share of all turns on which the LLM is called (re-weighted), per design
    fire = sum(weights[c["stratum"]] * (c["rule_done"] or c["rule_ask"]) for c in cases) / total_turns
    # the live gate makes one call when either pre-filter selects the turn, so cost uses the union
    pre_union = sum(weights[c["stratum"]] for c in cases
                    if prefiltered(c, "done") or prefiltered(c, "ask")) / total_turns
    for check, (_, key) in CHECKS.items():
        gold = {i: bool(r[key]) for i, r in gold_rows.items()}
        positives = sum(gold.values())
        out.append(f"\n### {check} check (reference positives in sample: {positives})\n")
        out.append("| judge | precision | recall | F1 | F1 95% CI | sample hit / false alarm / miss "
                   "| needless stops (% of all turns) | cost / 1k turns | added wait p50 (API / wall) |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for name, pred in predictions(cases, preds, check).items():
            p, r, f = weighted_prf(cases, weights, gold, pred)
            lo, hi = bootstrap_f1(cases, weights, gold, pred)
            ids = [c["id"] for c in cases if c["id"] in pred and c["id"] in gold]
            raw = (sum(pred[i] and gold[i] for i in ids), sum(pred[i] and not gold[i] for i in ids),
                   sum(gold[i] and not pred[i] for i in ids))
            needless = sum(weights[c["stratum"]] for c in cases
                           if c["id"] in ids and pred[c["id"]] and not gold[c["id"]]) / total_turns
            if name.startswith("rule"):
                cost, lat = "$0", "0 s"
            else:
                model = name.split(":")[1] if ":" in name else name.split("+")[1]
                rows = list(preds[model].values())
                per_call = statistics.mean(x.get("cost_usd", 0) for x in rows) if rows else 0
                api = statistics.median(x.get("api_ms") or 0 for x in rows) / 1000 if rows else 0
                wall = statistics.median(x.get("wall_ms") or 0 for x in rows) / 1000 if rows else 0
                share = 1.0 if name.startswith("llm") else pre_union if name.startswith("prefilter") else fire
                cost = f"${1000 * per_call * share:.2f}"
                lat = f"{api:.1f} / {wall:.1f} s" + ("" if share == 1.0 else f" on {100 * share:.0f}% of turns")
            out.append(f"| {name} | {fmt(p)} | {fmt(r)} | {fmt(f)} | {fmt(lo)}–{fmt(hi)} | {raw[0]} / {raw[1]} / {raw[2]} "
                       f"| {100 * needless:.1f}% | {cost} | {lat} |")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
