"""Decide whether a shell command writes files.

Agents do not only edit through Edit/Write tools. In practice they also edit
with redirects, `sed -i`, `cp`/`mv`, and, very often, with an inline script:

    python3 - <<'EOF'
    p = pathlib.Path("app.py"); p.write_text(p.read_text().replace(...))
    EOF

A gate that only watches Edit/Write misses all of these. This module is the
single place that answers "did this Bash call write a file?", so every gate
agrees on what counts as an edit.

The detection is deliberately conservative toward "write": a false "write"
only costs one extra check, a missed write silently disables a gate.
"""
from __future__ import annotations

import os
import re
import shlex

# Paths that are scratch space, not the user's work.
SCRATCH_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")

_INLINE_INTERP = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:python(?:3(?:\.\d+)?)?|node|ruby|perl|deno|bun)\b[^\n]*?(?:\s-[ceE]\b|<<)",
    re.M)
_INLINE_WRITE = re.compile(
    r"\.write_(?:text|bytes)\(|\.open\(\s*['\"][wax]|\bopen\([^)\n]*,\s*(?:mode\s*=\s*)?['\"](?:[wax]|r\+)"
    r"|\bshutil\.(?:copy\w*|move|rmtree)\(|\bos\.(?:replace|rename|remove|unlink)\(|\.(?:rename|unlink)\("
    r"|\b(?:writeFileSync|appendFileSync|writeFile|appendFile|rmSync|unlinkSync|renameSync)\(|\bFile\.write\(")
_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


def _is_scratch(path: str) -> bool:
    if not path or path == "/dev/null" or path.startswith("&"):
        return True
    real = os.path.realpath(os.path.expanduser(path))
    return any(real == r or real.startswith(r + "/") for r in SCRATCH_ROOTS)


def inline_script_writes(command: str) -> bool:
    """An inline interpreter script (-c / -e / heredoc) that calls a file-writing API."""
    return bool(_INLINE_INTERP.search(command) and _INLINE_WRITE.search(command))


def _segment_writes(tokens: list[str]) -> bool:
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _REDIRECTS and not _is_scratch(nxt):
            return True
    words = [t for t in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
    if not words:
        return False
    name = os.path.basename(words[0])
    args = [w for w in words[1:] if not w.startswith("-")]
    if name == "tee":
        return any(not _is_scratch(a) for a in args)
    if name in ("sed", "perl") and any(re.match(r"^-[A-Za-z]*i", w) or w.startswith("--in-place")
                                       for w in words[1:]):
        return any(not _is_scratch(a) for a in args[1:]) if args else True
    if name in ("cp", "mv", "install") and args:
        return not _is_scratch(args[-1])
    return False


_FILE_LIKE = re.compile(r"^[^\s'\"]*[^/\s]\.[A-Za-z0-9]{1,8}$")
_QUOTED_FILE = re.compile(r"['\"]([^'\"\s]+\.[A-Za-z0-9]{1,8})['\"]")


def _segment_targets(tokens: list[str]) -> list[str]:
    out = []
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _REDIRECTS and not _is_scratch(nxt):
            out.append(nxt)
    words = [t for t in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
    if not words:
        return out
    name = os.path.basename(words[0])
    args = [w for w in words[1:] if not w.startswith("-")]
    if name == "tee":
        out += [a for a in args if not _is_scratch(a)]
    elif name in ("sed", "perl") and any(re.match(r"^-[A-Za-z]*i", w) or w.startswith("--in-place") for w in words[1:]):
        # skip the BSD backup suffix ('') and the edit script (s/a/b/): keep file-like arguments
        out += [a for a in args if _FILE_LIKE.match(a) and not _is_scratch(a)] or [""]
    elif name in ("cp", "mv", "install") and args and not _is_scratch(args[-1]):
        out.append(args[-1])
    return out


def written_paths(command: str) -> list[str] | None:
    """Best-effort list of the files a command writes, or None when a write
    target cannot be read off the command (an inline script without a literal
    file name, a variable). Callers must treat None as "may write anything"."""
    if not command_writes(command):
        return []
    targets: list[str] = []
    if inline_script_writes(command):
        found = _QUOTED_FILE.findall(command)
        if not found:
            return None
        targets += found
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segment: list[str] = []
    for tok in tokens + [";"]:
        if tok and set(tok) <= set(";&|()\n"):
            targets += _segment_targets(segment)
            segment = []
        else:
            segment.append(tok)
    if any("$" in t or not t for t in targets):  # a variable, or a target we could not read
        return None
    return targets or None


def command_writes(command: str) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if inline_script_writes(command):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False  # unparsable: do not guess
    segment: list[str] = []
    for tok in tokens + [";"]:
        if tok and set(tok) <= set(";&|()\n"):
            if _segment_writes(segment):
                return True
            segment = []
        else:
            segment.append(tok)
    return False
