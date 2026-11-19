#!/usr/bin/env python3
"""PreToolUse(Bash): stop unquoted globs in places where zsh expands them first.

zsh expands an unquoted glob before the command sees it. With no match it
aborts ("no matches found"); with a match it silently passes file names
instead of the pattern. Both are easy to miss for an agent that writes
`grep -r --include=*.py`, `find . -name *.md` or `rg -g *.ts`.

Only forms where the meaning is certain are blocked. Commands whose word
boundaries are uncertain ($(...), backticks, unclosed quotes, eval) are let
through and logged as "unsure". Fail-open on any error.
Kill switch: EVIDENCE_GATES_DISABLE=zsh_glob.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hookio  # noqa: E402

GATE = "zsh_glob"
REASON = ("The glob in `{word}` is not quoted. zsh expands it before the command runs: with no match the "
          "command aborts with \"no matches found\", with a match it silently gets file names instead of the "
          "pattern. Quote the pattern, for example `'*.py'`.")

GLOB_CHARS = set("*?[")
FIND_OPTS = {"-name", "-iname", "-path", "-ipath"}
RG_OPTS = {"-g", "--glob"}
WRAPPERS = {"sudo", "env", "time", "nohup", "command", "exec", "xargs", "timeout", "nice", "caffeinate"}
# cheap pre-filter: commands without any of these are not tokenized
HINT = re.compile(r"--[A-Za-z0-9][\w-]*=|--glob\b|-i?name\b|-i?path\b|(?:^|\s)-g")
CANDIDATE = re.compile(r"(--[A-Za-z0-9][\w-]*=|-i?name\s+|-i?path\s+|(?:^|\s)-g\s*|--glob\s+)[^\s'\"]*[*?\[]")


class Unsure(Exception):
    pass


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (<<EOF, <<'EOF', <<-EOF): their text is data, not command words."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tags = re.findall(r"(?<!<)<<-?(?!<)\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)   # <<< is a here-string
        i += 1
        for tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1   # the closing tag line
    return "\n".join(out)


def tokenize(cmd: str) -> list:
    """Split into commands of (word, has_unquoted_glob, glob_positions).
    Raises Unsure for forms whose word boundaries are not certain
    ($(...), backticks, unclosed quotes)."""
    cmds, words = [], []
    cur, globby, in_word, gpos = [], False, False, []
    i, n = 0, len(cmd)

    def end_word():
        nonlocal cur, globby, in_word, gpos
        if in_word:
            words.append(("".join(cur), globby, tuple(gpos)))
        cur, globby, in_word, gpos = [], False, False, []

    def end_cmd():
        nonlocal words
        end_word()
        if words:
            cmds.append(words)
        words = []

    while i < n:
        ch = cmd[i]
        if ch in " \t":
            end_word()
            i += 1
        elif ch in ";\n" or ch in "&|()":
            end_cmd()
            i += 1
        elif ch == "#" and not in_word:
            while i < n and cmd[i] != "\n":
                i += 1
        elif ch == "\\":
            if i + 1 < n:
                cur.append(cmd[i + 1])
                in_word = True
            i += 2
        elif ch == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise Unsure("unclosed '")
            cur.append(cmd[i + 1:j])
            in_word = True
            i = j + 1
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    buf.append(cmd[j + 1])
                    j += 2
                    continue
                if cmd[j] == "`" or cmd.startswith("$(", j):
                    raise Unsure("command substitution inside quotes")
                buf.append(cmd[j])
                j += 1
            if j >= n:
                raise Unsure('unclosed "')
            cur.append("".join(buf))
            in_word = True
            i = j + 1
        elif ch == "`" or cmd.startswith("$(", i):
            raise Unsure("command substitution")
        elif ch == "$" and i + 1 < n and cmd[i + 1] == "'":
            j = cmd.find("'", i + 2)
            if j < 0:
                raise Unsure("unclosed $'")
            cur.append(cmd[i + 2:j])
            in_word = True
            i = j + 1
        else:
            if ch in GLOB_CHARS:
                globby = True
                gpos.append(sum(len(x) for x in cur))   # position of the unquoted glob char in the word
            cur.append(ch)
            in_word = True
            i += 1
    end_cmd()
    return cmds


CASE_BLOCK = re.compile(r"\bcase\b[\s\S]*?\bin\b[\s\S]*?\besac\b")
NONOMATCH = re.compile(r"\b(setopt\s+(no_?nomatch|null_?glob)|unsetopt\s+nomatch)\b", re.I)


def decide(cmd: str, shell: str = "zsh") -> tuple:
    """Return ("block" | "pass" | "unsure", rule, word).

    Blocked forms (the ones where zsh is certain to misbehave):
      - a word starting with `--` with an unquoted glob after `=`
        (--include=*.py). No file name starts with `--`, so under zsh's default
        NOMATCH this always fails with "no matches found".
      - the word after find -name / -iname / -path / -ipath
      - the word after rg -g / --glob (and the glued form -g*.ts)
    In bash/sh, or after `setopt nonomatch` / with `noglob`, an unmatched glob
    is passed through literally, so the `--x=*` form is only reported as unsure."""
    if not isinstance(cmd, str) or not (GLOB_CHARS & set(cmd)) or not HINT.search(cmd):
        return "pass", "", ""
    literal_ok = shell in ("bash", "sh") or bool(NONOMATCH.search(cmd))
    # Patterns in `case ... in pat)` and inside `[[ ... ]]` are matched, not expanded.
    pattern_ctx = bool(CASE_BLOCK.search(cmd)) or "[[" in cmd
    text = _strip_heredocs(cmd)
    try:
        cmds = tokenize(text)
    except Unsure as e:
        m = CANDIDATE.search(text)
        return ("unsure", str(e), m.group(0)) if m else ("pass", "", "")
    unsure = None
    for words in cmds:
        names = [os.path.basename(w[0]) for w in words]
        if "noglob" in names[:1] or (names[:1] and names[0] in WRAPPERS and "noglob" in names[:3]):
            continue   # zsh noglob: this command does not expand globs
        if "eval" in names:
            m = CANDIDATE.search(" ".join(w[0] for w in words))
            if m:
                unsure = ("unsure", "eval", m.group(0))
            continue
        for k, (w, g, gpos) in enumerate(words):
            eq = w.find("=")
            if g and w.startswith("--") and eq > 2 and any(pos > eq for pos in gpos):
                if pattern_ctx:
                    unsure = unsure or ("unsure", "case or [[ pattern", w)
                    continue
                if literal_ok:
                    unsure = unsure or ("unsure", "nonomatch or bash", w)
                    continue
                return "block", "prefix", w
            if w in FIND_OPTS and "find" in names[:k] and k + 1 < len(words) and words[k + 1][1]:
                return "block", "find", f"{w} {words[k + 1][0]}"
            if "rg" in names[:k]:
                if w in RG_OPTS and k + 1 < len(words) and words[k + 1][1]:
                    return "block", "rg", f"{w} {words[k + 1][0]}"
                if g and w.startswith("-g") and len(w) > 2 and not w.startswith("--"):
                    return "block", "rg", w
    return unsure or ("pass", "", "")


def command_and_shell(event: dict) -> tuple[str, str]:
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    v = inp.get("command", inp.get("cmd"))
    if isinstance(v, list) and v and all(isinstance(s, str) for s in v):
        if len(v) >= 3 and v[-2] in ("-c", "-lc"):
            return v[-1], os.path.basename(v[0])
        return " ".join(v), "zsh"
    shell = os.path.basename(os.environ.get("SHELL", "zsh")) or "zsh"
    return (v if isinstance(v, str) else ""), shell


def main() -> int:
    if hookio.disabled(GATE):
        return 0
    event = hookio.read_event()
    if event is None or event.get("tool_name") != "Bash":
        return 0
    try:
        cmd, shell = command_and_shell(event)
        verdict, rule, word = decide(cmd, shell)
    except Exception:
        return 0
    session = str(event.get("session_id", ""))
    if verdict == "unsure":
        hookio.log_event(GATE, "unsure", session, rule)
    elif verdict == "block":
        hookio.log_event(GATE, "block", session, rule)
        hookio.deny_tool(REASON.format(word=word[:80]))
    else:
        hookio.log_event(GATE, "pass", session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
