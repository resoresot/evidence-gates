# evidence-gates — stop a coding agent from ending a task without evidence

**A Claude Code plugin that catches three common ways an AI coding agent ends a task badly, at the moment it tries to stop, and sends it back to fix them.**

| Failure | Typical example |
|---|---|
| Stopping while tests fail | a fix broke a test, and the agent still says "done" |
| Asking what it could decide itself | "Shall I proceed with this approach?", and the human just types "yes" |
| Saying "done" without checking | edits code, runs nothing, reports "fixed" |

- **For:** people who hand work to Claude Code and keep getting "done" when it isn't, or a stalled permission question.
- **How:** Claude Code hooks (scripts that run at fixed moments) check the end of each turn and hand a reason back to
  the agent. The rules come from mistakes that kept recurring after they were written down as instructions.
- **Measured:** whether a regex rule or an LLM should judge was measured on labels ([eval/RESULTS.md](eval/RESULTS.md),
  Japanese), and the defaults follow the result (the unverified-"done" rule was mostly wrong, so it only logs by default).

[日本語](README.md)

## Why

Instructions such as "run the tests before you say done" in CLAUDE.md are followed only when the agent
remembers them. Hooks run every time, at fixed moments, and a blocked hook hands its reason back to the agent.
This plugin moves the rules that were repeatedly broken into hooks.

## What is inside

| Part | Runs | Blocks | Escape hatch |
|---|---|---|---|
| `completion_gate` / done | on Stop | claiming completion when no code was run after the last edit; edits through the shell (`sed -i`, redirects, inline `python3 - <<EOF … write_text`) count as edits, and look-only commands (`ls`, `git status`) do not count as checks. **The rule version only logs by default** (measured to be mostly wrong; see below) | state "unverified" and how to check |
| `completion_gate` / ask | on Stop | ending on a permission question the agent can decide, or on "I'll check." without checking | last line `Reason to ask: irreversible / external / preference / scope / cost / missing-info` (or the Japanese `確認理由: 不可逆 / 外部送信 / 好み / スコープ拡大 / コスト / 情報不足`) |
| `completion_gate` / lang | on Stop | answering a Japanese-speaking user in English | — |
| `test_gate` | on Stop | stopping while the repo's tests fail (opt-in per repo via `.claude/test_gate.json`, tests chosen by changed files) | gives up after `max_iterations` failures (default 3) and tells the human |
| `zsh_glob_gate` | before Bash | unquoted globs that zsh expands first (`grep --include=*.py`, `find -name *.md`, `rg -g *.ts`) | uncertain forms, and patterns inside `case` / `[[ ]]`, are only logged |
| `lessons` | on each prompt | shows up to 3 recorded lessons relevant to the request | — |

The done and ask checks can also be judged by an LLM (`EVIDENCE_GATES_JUDGE=sonnet`). Claude Code's
`type: "prompt"` hooks see only the last reply, not what ran after the last edit, so the gate builds that
context itself (request, compacted actions, final reply) and calls the Claude Code CLI with no tools, hooks or
settings, only on turns a cheap pre-filter selects. If the model cannot be asked, the rule decides.

All gates are fail-open. `completion_gate` never blocks twice in a row. Every decision is one line in
`events.jsonl` (no prompt or reply text), and `tools/stats.py` turns it into weekly fire rates, so a gate that
silently stops working shows up.

Loop: record a lesson (`eg-lessons add`) → it is shown when relevant → count recurrences (`eg-lessons recur`)
→ `eg-lessons report` lists lessons that keep recurring → implement them as a hook → mark them as gated.

## Does it work? (measured)

`tools/replay.py` replays the gates over past transcripts and reports counts only. On the author's own
Claude Code history (each row uses the period before that gate existed, so the windows differ):

- `zsh_glob_gate`: of 15,003 Bash calls, 197 would be blocked; **176 (89.3%) really failed with
  `no matches found`**; the other 21 may include globs that matched and silently changed meaning
  (not checked). It catches 62.2% of the 283 such failures of search commands.
- `completion_gate` / done: while an older version that only watched edit tools was live, 120 unverified
  "done" claims got through (117 of them through shell edits), against 145 verified ones. The user's next
  message complained in **4.2% vs 3.4%**: no clear difference. With this crude signal the gate shows no
  measurable effect yet, so it was re-measured against labels (next paragraph).
- `completion_gate` / ask (before it existed): fired on 9 of 241 turns; 2 were answered with a bare "yes".

Rule vs LLM, on labels ([eval/RESULTS.md](eval/RESULTS.md), Japanese): 178 turns sampled by stratum from 1,692 of
the author's turns, labelled by Opus 5.5 and checked against a second labeler from another vendor (an OpenAI GPT model) and
the author, plus 112 public synthetic cases.

- done: only 1 of 178 turns was a real unverified "done". The rule blocked about 5% of all turns and all 28
  sampled blocks were unneeded by the reference, mostly because an edit and its check were chained in one shell
  command. So the rule now only logs by default (`EVIDENCE_GATES_DONE_RULE=block` restores it). On synthetic
  cases a pre-filter + Sonnet caught all 30 positives, the rule 13.
- ask: on real history the rule and the LLM are not distinguishable: of 5 positives both catch 2 and miss 3
  (weighted F1 7% vs 72% comes from a single heavily weighted case). Synthetic: 63% vs 77%. The rule stays the
  default. If you use the LLM, use Sonnet (about $8 per 1,000 turns, ~3 s on the turns it is called); Haiku
  stopped needlessly on 6.4% of all turns.
- The judges themselves were tested with injected failures of known answer: removing the check from verified
  cases, the rule caught 7 of 23 and Sonnet all 23. Judged five times, done verdicts were the same on 99% of cases,
  while ask verdicts flipped on nearly half of the positives.
- Decisions and the options turned down are in [docs/adr/](docs/adr/) (Japanese).
- Three measuring bugs were found and fixed on the way; one (commands cut at 160 characters before judging)
  had made 7 of 9 inspected reference labels wrong.

End-to-end check in real Claude Code (run when the done rule still blocked by default; today that is
`EVIDENCE_GATES_DONE_RULE=block`): asked to fix a bug and "just say it is fixed without running tests",
the agent was stopped by `completion_gate`, rewrote its report as unverified with how to check, and
`test_gate` ran the tests on stop and saw them pass.

Design choice: on the author's setup, 8 small tasks (once each) and one task touching 6+ files (3 times each)
were solved three ways: no hooks, hooks, and a multi-stage pipeline that agrees a change contract up front,
implements, verifies mechanically and repairs once. Held-out tests were hidden from the model. A strong model
(Opus 5.5) passed every held-out test in all three; small tasks cost $0.22 / $0.33 / $0.38 each (the hooks
figure excludes work handed to an external implementation tool). Small sample, but up-front contracts and
routing only added cost and time at this scale, so the plugin adds no up-front process, only checks at the end.
The same experiment found agents editing files through inline shell scripts and slipping past edit-watching
gates, which `shell_writes.py` now covers.

## Trust boundary and fit

- The gates stop an agent's slips, not a malicious agent. It can edit `.claude/test_gate.json`, and writing
  "unverified" passes the done check; the latter is a deliberate escape hatch for honest reporting.
- `test_gate` runs a shell command stored in the repository. Do not enable it for untrusted repositories.
- The ask check pushes toward fewer needless questions. For business agents built around approvals and
  permissions, a missed question costs more; use the categories the other way round there (always ask).

## Publish check (`tools/publish_check.py`)

Checks exactly the files and commits that would be published: a private denylist kept outside the repo (terms
are never printed), generic personal data, secrets (built-in patterns + gitleaks), commit identities, every past
version of every file, bidirectional control characters, and files that should never be published. Before this
repository was published it caught a literal bidi character in the checker's own source, an identifier that
only meant something on the author's machine in a test fixture, and a git author name that was not the public
account name.

## Install

```bash
claude plugin marketplace add resoresot/evidence-gates
claude plugin install evidence-gates@evidence-gates
```

Python 3.10+ standard library only (the LLM mode uses the Claude Code CLI). Tests: `cd tests && python3 -m unittest`.

| Variable | Meaning |
|---|---|
| `EVIDENCE_GATES_DISABLE` | `all`, or any of `completion,test,zsh_glob,lessons` |
| `EVIDENCE_GATES_SKIP` | any of `done,ask,lang` inside completion |
| `EVIDENCE_GATES_JUDGE` | `rule` (default) or a model name such as `sonnet` (`haiku` stopped needlessly too often in the evaluation) |
| `EVIDENCE_GATES_DONE_RULE` | `shadow` (default: the done rule only logs) or `block` |

## Limits

The rules cover completion phrases in Japanese and part of English only. The numbers come from one person's
history with few positives (done 1, ask 5), so intervals on real data are wide. Whether a question was needed
depends on the user; the author was stricter than the Opus reference. Lesson relevance is word overlap and
misses paraphrases.
