#!/usr/bin/env python3
"""Stop hook: check how the agent ends its turn.

Three checks, each aimed at a failure that written instructions did not stop:

  done  The reply claims the work is finished, but nothing was run after the
        last edit (no test, no command, no browser check). Edits made through
        the shell (redirects, sed -i, inline python) count as edits too.
  ask   The reply ends by asking permission for something the agent could
        decide itself, or ends on "I will check X." without doing it.
  lang  The user writes in Japanese but the reply body is in English.

On a hit the hook blocks the stop once and tells the agent what to fix. It
never blocks twice in a row (`stop_hook_active`), so it cannot loop.

Escape hatches the agent can use when the gate is wrong for this turn:
  - an honest "unverified" / 「未検証」 label passes the done check
  - a last line `Reason to ask: <category>` (or 「確認理由: <分類>」) passes the
    ask check, for questions that really need the human
Kill switch: EVIDENCE_GATES_DISABLE=completion, or per check
EVIDENCE_GATES_SKIP=done,ask,lang.
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hookio  # noqa: E402
import llm_judge  # noqa: E402
from shell_writes import command_writes, written_paths  # noqa: E402
from transcript import load_turn  # noqa: E402

GATE = "completion"

ASK_CATEGORIES_EN = ("irreversible", "external", "preference", "scope", "cost", "missing-info")
ASK_CATEGORIES_JA = ("不可逆", "外部送信", "好み", "スコープ拡大", "コスト", "情報不足")
REASON_TAG = re.compile(
    r"(?im)^\s*(?:reason to ask|確認理由)\s*[:：]\s*(" + "|".join(ASK_CATEGORIES_EN + ASK_CATEGORIES_JA) + r")\b")

ASK_END = re.compile(
    r"(ますか|ませんか|でしょうか|ましょうか|ですか|いいですか|よろしいですか|良いですか"
    r"|いい|OK|ok|どうする|どうしますか|どちらにしますか|どっち)\s*[？?]\s*$"
    r"|(should i|shall i|do you want me to|want me to|would you like me to|ok to proceed)[^\n]*\?\s*$",
    re.I)
# "I'll check it." and the turn ends. Only verbs of checking, because broad
# verb lists misfire on ordinary plans ("next I will ...").
ANNOUNCE_END = re.compile(
    r"(確認|検証|実測)(します|してみます|していきます)[。．.!！]?\s*$"
    r"|(確かめ|調べ|見て(み|いき))(ます|てみます)[。．.!！]?\s*$"
    r"|\b(i'?ll|i will|let me) (check|verify|look into|investigate)\b[^\n]*[.!]?\s*$",
    re.I)
CONDITIONAL = re.compile(r"(たら|れば|なら|ば、|あとで|後で|次回|次に|今後|ときは|場合は|ので、?$|\bif\b|\bonce\b|\bwhen\b)", re.I)
REQUEST_WORDS = re.compile(
    r"(ください|下さい|いかが|でしょうか|しますか|ますか|よろしければ|よければ|一言|お知らせ|教えて|選んで|決めて"
    r"|ご判断|ご確認|ご指示|お返事|\blet me know\b|\bshould i\b|\bwould you like\b|\bwant me to\b)", re.I)
LIST_ITEM = re.compile(r"^\s*([-*・•]|\d+[.)．、])\s*")

DONE_CLAIM = re.compile(
    r"(直しました|修正しました|完了しました|できました|実装しました|反映しました|対応しました"
    r"|デプロイしました|動くようになりました|直っています|解消しました|仕上げました|終わりました|修正済み"
    r"|(?<!not )\bfixed\b(?! yet)|(?<!not )\bimplemented\b(?! yet)|\ball set\b|\bworks now\b|(?<!not )\bdone\b(?! yet))",
    re.I)
UNVERIFIED = re.compile(r"未検証|\bunverified\b|\bnot verified\b", re.I)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Running a check in the same command as the write also verifies it.
RUNS_CHECK = re.compile(
    r"\b(pytest|unittest|vitest|jest|playwright|(npm|pnpm|yarn)( run)? test|go test|cargo test|make test)\b")

KANA = re.compile(r"[぀-ヿ]")
CJK = re.compile(r"[぀-ヿ㐀-鿿]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
CODE = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|\S+/\S+", re.S)


def last_line(text: str) -> str:
    body = CODE.sub(" ", text).strip()
    if not body:
        return ""
    para = [p for p in re.split(r"\n\s*\n", body) if p.strip()][-1]
    lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# Prose, backups and logs have nothing to run. Without this, a note or report written after a
# verified code change made the check fire wrongly (see eval/RESULTS.md).
PROSE = re.compile(r"(\.(md|markdown|txt|rst|adoc|org|log|bak|prev|orig|old)|~)$", re.I)


def is_edit(item: dict, count_prose: bool = False) -> bool:
    if item.get("type") != "tool_use":
        return False
    name = item.get("name")
    if name in EDIT_TOOLS:
        path = str((item.get("input") or {}).get("file_path") or (item.get("input") or {}).get("notebook_path") or "")
        return count_prose or not PROSE.search(path)
    if name != "Bash":
        return False
    command = str((item.get("input") or {}).get("command", ""))
    if not command_writes(command) or RUNS_CHECK.search(command):
        return False
    if count_prose:
        return True
    targets = written_paths(command)
    return targets is None or not all(PROSE.search(t.rstrip("\"'")) for t in targets)


# Commands that only look at files or the repo state do not exercise the change.
LOOK_ONLY = re.compile(
    r"^\s*(cd\s+\S+|ls|pwd|cat|head|tail|wc|echo|printf|grep|rg|find|tree|diff|stat|file"
    r"|git\s+(status|diff|log|show|add|commit|branch|rev-parse))\b")


def is_check(item: dict) -> bool:
    """Something that exercises the result after an edit: a command that runs
    code (tests, scripts, builds, requests), a browser or other MCP tool, or a
    delegated agent. Commands that only read files or git state do not count."""
    if item.get("type") != "tool_use":
        return False
    name = item.get("name", "")
    if name.startswith("mcp__") or name in ("Agent", "Task"):
        return True
    if name != "Bash":
        return False
    command = str((item.get("input") or {}).get("command", ""))
    parts = [p for p in re.split(r"&&|\|\||;|\||\n", command) if p.strip()]
    return bool(parts) and not all(LOOK_ONLY.match(p) for p in parts)


def check_done(final: str, items: list[dict], count_prose: bool = False) -> str | None:
    if not DONE_CLAIM.search(CODE.sub(" ", final)) or UNVERIFIED.search(final):
        return None
    edits = [i for i, c in enumerate(items) if is_edit(c, count_prose)]
    if not edits:
        return None
    if any(is_check(c) for c in items[edits[-1] + 1:]):
        return None
    return ("You report the work as finished, but nothing was run after your last edit "
            "(no test, command, or browser check). Run something that shows the result, then report "
            "what you observed. If it cannot be checked here, say \"unverified\" and state how to check it.")


def check_ask(final: str) -> str | None:
    if REASON_TAG.search(final):
        return None
    tail = last_line(re.sub(r"(?im)^\s*(?:reason to ask|確認理由)\s*[:：].*$", "", final))
    if ASK_END.search(tail):
        return ("Your reply ends by asking permission. If you can decide this yourself, decide and keep "
                "working. If it truly needs the human (irreversible, sends something outside, a matter of "
                "taste, a scope change, cost, or missing information), add a last line "
                "`Reason to ask: <category>`.")
    if ANNOUNCE_END.search(tail) and not CONDITIONAL.search(tail) and not LIST_ITEM.match(tail):
        return "Your reply ends by announcing a check without doing it. Do the check now, then report."
    return None


def check_lang(final: str, user_text: str) -> str | None:
    if not KANA.search(user_text or ""):
        return None
    if final.lstrip().startswith(("{", "[")):
        return None  # machine output (JSON) is not a reply to a person
    body = CODE.sub(" ", final)
    cjk = len(CJK.findall(body))
    latin = sum(len(w) for w in LATIN_WORD.findall(body))
    if latin < 200 or cjk / max(1, cjk + latin) >= 0.3:
        return None
    return "The user writes in Japanese, but your reply is in English. Write the same reply in Japanese."


DONE_MSG = ("You report the work as finished, but nothing was run after your last edit "
            "(no test, command, or browser check). Run something that shows the result, then report "
            "what you observed. If it cannot be checked here, say \"unverified\" and state how to check it.")
ASK_MSG = ("Your reply ends by asking permission or announcing a check. If you can decide or do this "
           "yourself, do it and keep working. If it truly needs the human (irreversible, sends something "
           "outside, a matter of taste, a scope change, cost, or missing information), add a last line "
           "`Reason to ask: <category>`.")


def prefilter(final: str, items: list[dict]) -> dict[str, bool]:
    """Turns where a check can apply at all; only these are sent to the LLM.

    Deliberately wider than the rules: a first version required one of the
    DONE_CLAIM phrases and missed most completion claims worded any other way
    (recall 44% on the synthetic set). Here the done check only needs an edit,
    and the ask check only needs a question-like or announcing last line."""
    body = re.sub(r"(?im)^\s*(?:reason to ask|確認理由)\s*[:：].*$", "", final)
    tail = last_line(body)
    paras = [p for p in re.split(r"\n\s*\n", CODE.sub(" ", body)) if p.strip()]
    # Japanese requests for a go-ahead rarely end in "?": 「進めるなら一言ください。」
    request = bool(paras and REQUEST_WORDS.search(paras[-1]))
    return {"done": any(is_edit(c, count_prose=True) for c in items),
            "ask": bool(re.search(r"[?？]\s*$", tail) or ASK_END.search(tail) or ANNOUNCE_END.search(tail) or request)}


JUDGE_LOG: list[str] = []  # what the LLM call did this turn, for the events log


def llm_checks(model: str, final: str, items: list[dict], user_text: str) -> dict[str, str] | None:
    """LLM verdicts for the pre-filtered checks; None if the model could not be asked."""
    pre = prefilter(final, items)
    if not any(pre.values()):
        JUDGE_LOG.append("judge=skipped")
        return {}
    t0 = time.time()
    verdict = llm_judge.judge(model, user_text, llm_judge.compact_actions(items), final)
    ms = int((time.time() - t0) * 1000)
    if verdict is None:
        JUDGE_LOG.append(f"judge=failed {ms}ms")
        return None
    JUDGE_LOG.append(f"judge=ok {ms}ms ${verdict.get('cost_usd') or 0:.4f}")
    out = {}
    if pre["done"] and verdict.get("done_unverified"):
        out["done"] = DONE_MSG
    if pre["ask"] and verdict.get("unneeded_question") and not REASON_TAG.search(final):
        out["ask"] = ASK_MSG
    return out


def evaluate(event: dict, done_rule: str | None = None) -> dict[str, str]:
    """Reasons keyed by check. A key ending in `_shadow` is logged but does not block.

    The rule version of the done check runs in shadow by default: on the author's history it
    fired on about 5% of turns and none of the 28 sampled firings was a real unverified claim (most
    were an edit and its check chained in one shell command, which the rule cannot read; see
    eval/RESULTS.md). EVIDENCE_GATES_DONE_RULE=block restores blocking; with an LLM judge
    configured, the judge's done verdict always blocks."""
    skip = {s.strip() for s in os.environ.get("EVIDENCE_GATES_SKIP", "").split(",") if s.strip()}
    if done_rule is None:
        done_rule = os.environ.get("EVIDENCE_GATES_DONE_RULE", "shadow").strip() or "shadow"
    final, items, user_text = load_turn(event.get("transcript_path"), event.get("last_assistant_message"))
    if not final.strip():
        return {}
    reasons: dict[str, str] = {}
    model = os.environ.get("EVIDENCE_GATES_JUDGE", "rule").strip()
    llm = None
    if model and model != "rule":
        try:
            llm = llm_checks(model, final, items, user_text)
        except Exception:
            llm = None  # fall back to the rules below
    for key, fn in (("lang", lambda: check_lang(final, user_text)),
                    ("done", lambda: llm.get("done") if llm is not None else check_done(final, items)),
                    ("ask", lambda: llm.get("ask") if llm is not None else check_ask(final))):
        if key in skip:
            continue
        try:
            r = fn()
        except Exception:
            r = None
        if r:
            shadow = key == "done" and llm is None and done_rule != "block"
            reasons[key + "_shadow" if shadow else key] = r
    return reasons


def main() -> int:
    if hookio.disabled(GATE):
        return 0
    event = hookio.read_event()
    if event is None:
        return 0
    if event.get("stop_hook_active"):
        # second stop right after a block: never block twice, but keep it in the counts
        hookio.log_event(GATE, "pass", str(event.get("session_id", "")), "after_block")
        return 0
    try:
        reasons = evaluate(event)
    except Exception:
        return 0
    session = str(event.get("session_id", ""))
    blocking = {k: v for k, v in reasons.items() if not k.endswith("_shadow")}
    detail = ";".join(x for x in (",".join(reasons), *JUDGE_LOG) if x)
    if blocking:
        hookio.log_event(GATE, "block", session, detail)
        hookio.block_stop("\n".join(blocking.values()))
    else:
        hookio.log_event(GATE, "pass", session, detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
