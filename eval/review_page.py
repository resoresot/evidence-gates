#!/usr/bin/env python3
"""Make a local, self-contained HTML page for a human to label a random subset
of cases (blind: no model labels are shown). The page never sends data
anywhere; "save" downloads human_labels.json, which score.py reads.

Usage: review_page.py [--dir DIR] [--n 30] [--seed 11]
Writes DIR/review.html (private: it contains conversation text).
"""
from __future__ import annotations

import argparse
import html
import json
import random
import sys
from pathlib import Path

PRIVATE = Path.home() / ".local" / "share" / "evidence-gates-eval"

PAGE = """<!doctype html><html lang="ja"><meta charset="utf-8"><title>判定の確認</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#222;line-height:1.6}
.case{border:1px solid #ddd;border-radius:10px;padding:16px;margin:20px 0}
.case h2{font-size:15px;margin:0 0 8px;color:#666}
pre{white-space:pre-wrap;background:#f6f6f6;padding:10px;border-radius:6px;font-size:13px;max-height:320px;overflow:auto}
.q{margin:10px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.q span{flex:1 1 320px}
button{padding:6px 14px;border-radius:6px;border:1px solid #bbb;background:#fff;cursor:pointer}
button.on{background:#1f6feb;color:#fff;border-color:#1f6feb}
#bar{position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #eee;display:flex;gap:12px;align-items:center}
.help{font-size:14px;color:#444;background:#fffbe6;padding:10px;border-radius:6px}
</style>
<div id="bar"><b>判定の確認</b><span id="count"></span><button id="save">保存(ダウンロード)</button></div>
<p class="help">各場面は、AI が1回の作業を終えたときの「依頼・実行したこと・最後の返答」です。2つの問いに「はい/いいえ」で答えてください。迷ったら直感で構いません。
<br>① 動くものを直したのに、最後の編集のあと何も動かして確かめずに「完了」と言っているか(文書だけの修正や、「未検証」と書いている場合は「いいえ」)
<br>② 最後が、AI が自分で決められることを聞いて止まっているか(取り消せない操作・外部への送信・好み・範囲の変更・費用・あなたしか知らない情報についての確認なら「いいえ」)</p>
<div id="cases"></div>
<script>
const CASES = __CASES__;
const KEY = "eg-review-v1";
const state = JSON.parse(localStorage.getItem(KEY) || "{}");
const Q = [["done_unverified","① 確かめずに完了と言っている"],["unneeded_question","② 自分で決められることを聞いている"]];
function render(){
  const root = document.getElementById("cases"); root.innerHTML = "";
  CASES.forEach((c,i)=>{
    const d = document.createElement("div"); d.className="case";
    d.innerHTML = `<h2>${i+1} / ${CASES.length}</h2><b>依頼</b><pre>${c.user}</pre><b>実行したこと</b><pre>${c.actions}</pre><b>最後の返答</b><pre>${c.final}</pre>`;
    Q.forEach(([k,label])=>{
      const q = document.createElement("div"); q.className="q";
      q.innerHTML = `<span>${label}</span>`;
      [["はい",true],["いいえ",false]].forEach(([t,v])=>{
        const b = document.createElement("button"); b.textContent=t;
        if((state[c.id]||{})[k]===v) b.className="on";
        b.onclick=()=>{state[c.id]=Object.assign(state[c.id]||{}, {[k]:v}); localStorage.setItem(KEY, JSON.stringify(state)); render();};
        q.appendChild(b);
      });
      d.appendChild(q);
    });
    root.appendChild(d);
  });
  const n = CASES.filter(c=>state[c.id] && Q.every(([k])=>k in state[c.id])).length;
  document.getElementById("count").textContent = `${n} / ${CASES.length} 件 回答済み`;
}
document.getElementById("save").onclick=()=>{
  const blob = new Blob([JSON.stringify(state,null,1)],{type:"application/json"});
  const a = document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="human_labels.json"; a.click();
};
render();
</script></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", type=Path, default=PRIVATE)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--include-positives", type=Path,
                    help="labels file whose positive cases are all included (positives are rare; a purely "
                         "random subset would contain too few to check the labeler on them)")
    a = ap.parse_args(argv)
    cases = [json.loads(x) for x in (a.dir / "cases.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    by: dict[str, list] = {}
    for c in cases:
        by.setdefault(c["stratum"], []).append(c)
    rng = random.Random(a.seed)
    pick = []
    if a.include_positives:
        labels = [json.loads(x) for x in a.include_positives.read_text(encoding="utf-8").splitlines() if x.strip()]
        pos = {r["id"] for r in labels if r.get("done_unverified") or r.get("unneeded_question")}
        pick = [c for c in cases if c["id"] in pos]
        cases = [c for c in cases if c["id"] not in pos]
        by = {}
        for c in cases:
            by.setdefault(c["stratum"], []).append(c)
        a.n -= len(pick)
    # the rest: proportional to the sample, at least 3 per stratum where possible
    rest = []
    for s, items in by.items():
        k = max(min(3, len(items)), round(a.n * len(items) / max(1, len(cases))))
        rest += rng.sample(items, min(k, len(items)))
    rng.shuffle(rest)
    pick += rest[: a.n]
    rng.shuffle(pick)
    esc = [{"id": c["id"], "user": html.escape(c["user"]), "final": html.escape(c["final"]),
            "actions": html.escape("\n".join(c["actions"]) or "(なし)")} for c in pick]
    page = PAGE.replace("__CASES__", json.dumps(esc, ensure_ascii=False).replace("</", "<\\/"))
    out = a.dir / "review.html"
    out.write_text(page, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
