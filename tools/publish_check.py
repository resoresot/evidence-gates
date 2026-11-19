#!/usr/bin/env python3
"""Check a repository before it goes public.

What leaks from a personal repository is rarely a password. It is the author's
home path in a config file, a private project's name in a comment, a work
email in the commit metadata, or a word only the author uses. This tool looks
for all of those, mechanically, over exactly the files that would be pushed
and over every commit that would be pushed with them.

Checks (each reports PASS / WARN / FAIL):
  denylist   private terms from a list kept OUTSIDE the repo (names, project
             names, internal vocabulary). The list itself is never printed:
             findings show only which entry matched, masked.
  pii        generic personal data: e-mail addresses, home-directory paths,
             phone numbers, postal codes, private-network hosts.
  secrets    built-in key patterns, plus gitleaks over the files and history.
  identity   author / committer names and e-mails of every commit, and the
             identity the next commit will use.
  history    the denylist / pii / secret checks again over every past version
             of every file (a leak removed in a later commit is still public).
  unicode    bidirectional control characters (Trojan Source).
  files      files that should never be published (.env, keys, transcripts),
             binaries, and anything over the size limit.

Denylist format (one entry per line, `#` comments):
  some-term            case-insensitive literal
  re:\\bTKT-\\d{4}\\b  regular expression
  warn:other-term      reported, but does not fail the check

Usage:
  publish_check.py [REPO] [--denylist FILE] [--require-denylist]
                   [--allow-identity NAME_OR_EMAIL ...] [--max-kb 512]
Exit status: 0 = no FAIL, 1 = at least one FAIL, 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DENYLIST = Path(os.environ.get("PUBLISH_CHECK_DENYLIST")
                        or Path.home() / ".config" / "publish-check" / "denylist.txt")

# no-reply addresses, and the names reserved for examples (RFC 2606 / RFC 6761)
ALLOWED_EMAIL = re.compile(r"(@users\.noreply\.github\.com|@example\.(com|org|net)|noreply@anthropic\.com"
                           r"|^git@github\.com|\.(test|example|invalid|localhost))$", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HOME_PATH = re.compile(r"(/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)([A-Za-z0-9._-]+)")
PLACEHOLDER_USERS = {"you", "user", "username", "me", "name", "runner", "example", "shared", "<you>"}
PHONE = re.compile(r"(?<![\d-])(?:\+81[- ]?\d{1,4}|0\d{1,4})-\d{1,4}-\d{4}(?![\d-])")
POSTAL = re.compile(r"〒\s*\d{3}-?\d{4}")
PRIVATE_HOST = re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b|\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
                          r"|\bntfy\.sh/[A-Za-z0-9_-]{6,}", re.I)
SECRET_PATTERNS = {
    "anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai key": r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}",
    "github token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}",
    "aws key id": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "google api key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
}
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f]")
FORBIDDEN_NAMES = re.compile(r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.pem|.*\.p12|.*\.key|\.DS_Store"
                             r"|.*\.sqlite3?|.*\.db|credentials(\.json)?|settings\.local\.json)$", re.I)
TRANSCRIPT_NAME = re.compile(r"(^|/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")


@dataclass
class Report:
    results: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # check -> [(level, message)]

    def add(self, check: str, level: str, message: str) -> None:
        self.results.setdefault(check, []).append((level, message))

    def touch(self, check: str) -> None:
        self.results.setdefault(check, [])

    def failed(self) -> bool:
        return any(level == "FAIL" for items in self.results.values() for level, _ in items)

    def render(self) -> str:
        out = []
        for check, items in self.results.items():
            fails = sum(1 for lv, _ in items if lv == "FAIL")
            warns = sum(1 for lv, _ in items if lv == "WARN")
            status = "FAIL" if fails else "WARN" if warns else "PASS"
            out.append(f"[{status}] {check}" + (f"  ({fails} fail, {warns} warn)" if items else ""))
            for level, msg in items[:50]:
                out.append(f"    {level}: {msg}")
            if len(items) > 50:
                out.append(f"    ... {len(items) - 50} more")
        out.append("RESULT: " + ("FAIL" if self.failed() else "PASS"))
        return "\n".join(out)


# ---------------------------------------------------------------- denylist
@dataclass
class Entry:
    label: str          # "denylist line N" (never the term itself)
    pattern: re.Pattern
    warn_only: bool


def mask(term: str) -> str:
    return term[0] + "*" * max(1, len(term) - 2) + (term[-1] if len(term) > 1 else "")


def load_denylist(path: Path) -> list[Entry]:
    entries = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        warn = line.startswith("warn:")
        if warn:
            line = line[5:].strip()
        if line.startswith("re:"):
            pat = re.compile(line[3:], re.I)
            shown = f"regex #{n}"
        else:
            pat = re.compile(re.escape(line), re.I)
            shown = f"#{n} {mask(line)}"
        entries.append(Entry(f"denylist {shown}", pat, warn))
    return entries


# ---------------------------------------------------------------- scanning text
def scan_text(where: str, text: str, deny: list[Entry], report: Report, prefix: str = "") -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        loc = f"{prefix}{where}:{lineno}"
        for e in deny:
            if e.pattern.search(line):
                report.add(prefix + "denylist", "WARN" if e.warn_only else "FAIL", f"{loc} matches {e.label}")
        for m in EMAIL.finditer(line):
            if not ALLOWED_EMAIL.search(m.group(0)):
                report.add(prefix + "pii", "FAIL", f"{loc} e-mail address {mask(m.group(0))}")
        for m in HOME_PATH.finditer(line):
            if m.group(2).lower() not in PLACEHOLDER_USERS:
                report.add(prefix + "pii", "FAIL", f"{loc} home-directory path ({m.group(1)}{mask(m.group(2))})")
        if PHONE.search(line):
            report.add(prefix + "pii", "FAIL", f"{loc} phone number")
        if POSTAL.search(line):
            report.add(prefix + "pii", "FAIL", f"{loc} postal code")
        if PRIVATE_HOST.search(line):
            report.add(prefix + "pii", "FAIL", f"{loc} private host / tailnet address / notification topic")
        for name, pat in SECRET_PATTERNS.items():
            if re.search(pat, line):
                report.add(prefix + "secrets", "FAIL", f"{loc} possible {name}")
        if not prefix and BIDI.search(line):
            report.add("unicode", "FAIL", f"{loc} bidirectional control character")


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def publish_files(repo: Path) -> list[str]:
    if git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true":
        out = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return sorted({f for f in out.split("\0") if f and (repo / f).is_file()})
    return sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def check_files(repo: Path, files: list[str], deny: list[Entry], report: Report, max_kb: int) -> None:
    for c in ("denylist", "pii", "secrets", "unicode", "files"):
        report.touch(c)
    for rel in files:
        path = repo / rel
        for e in deny:  # the path itself can leak (a folder named after a client)
            if e.pattern.search(rel):
                report.add("denylist", "WARN" if e.warn_only else "FAIL", f"path {rel} matches {e.label}")
        if FORBIDDEN_NAMES.search(rel) or TRANSCRIPT_NAME.search(rel):
            report.add("files", "FAIL", f"{rel} is a kind of file that should not be published")
        size = path.stat().st_size
        if size > max_kb * 1024:
            report.add("files", "FAIL", f"{rel} is {size // 1024} KB (limit {max_kb} KB)")
        data = path.read_bytes()
        if is_binary(data):
            report.add("files", "WARN", f"{rel} is binary (not scanned as text)")
            continue
        scan_text(rel, data.decode("utf-8", errors="replace"), deny, report)


def check_identity(repo: Path, deny: list[Entry], allowed: list[str], report: Report) -> None:
    report.touch("identity")
    if git(repo, "rev-parse", "--is-inside-work-tree").strip() != "true":
        return
    people = set()
    log = git(repo, "log", "--all", "--format=%an\t%ae\t%cn\t%ce")
    for line in log.splitlines():
        an, ae, cn, ce = (line.split("\t") + ["", "", "", ""])[:4]
        people |= {("commit author", an, ae), ("commit committer", cn, ce)}
    name = git(repo, "config", "user.name").strip()
    email = git(repo, "config", "user.email").strip()
    people.add(("next commit (git config)", name, email))
    for role, n, e in sorted(people):
        if not n and not e:
            continue
        for e_ in deny:
            if e_.pattern.search(n) or e_.pattern.search(e):
                report.add("identity", "WARN" if e_.warn_only else "FAIL", f"{role} matches {e_.label}")
        if e and not ALLOWED_EMAIL.search(e) and e not in allowed:
            report.add("identity", "FAIL", f"{role} e-mail {mask(e)} is not a no-reply address")
        if allowed and n and n not in allowed:
            report.add("identity", "FAIL", f"{role} name {mask(n)} is not in --allow-identity")
        if re.search(r"[^\x00-\x7f]", n) and not allowed:
            report.add("identity", "WARN", f"{role} name {mask(n)} is non-ASCII; is it a real name or a brand?")


def check_history(repo: Path, deny: list[Entry], report: Report) -> None:
    report.touch("history")
    if not git(repo, "rev-parse", "--verify", "HEAD").strip():
        return
    # every version of every file ever committed on any branch
    blobs = git(repo, "rev-list", "--all", "--objects")
    seen = set()
    for line in blobs.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2 or parts[0] in seen:
            continue
        sha, path = parts
        if git(repo, "cat-file", "-t", sha).strip() != "blob":
            continue
        seen.add(sha)
        data = subprocess.run(["git", "-C", str(repo), "cat-file", "-p", sha], capture_output=True).stdout
        if is_binary(data):
            continue
        sub = Report()
        scan_text(f"{path}@{sha[:8]}", data.decode("utf-8", errors="replace"), deny, sub, prefix="history:")
        for items in sub.results.values():
            for level, msg in items:
                report.add("history", level, msg.replace("history:", "", 1))
    for msg in git(repo, "log", "--all", "--format=%H%x00%B%x01").split("\x01"):
        if "\0" not in msg:
            continue
        sha, body = msg.split("\0", 1)
        sub = Report()
        scan_text(f"commit message {sha.strip()[:8]}", body, deny, sub, prefix="history:")
        for items in sub.results.values():
            for level, m in items:
                report.add("history", level, m.replace("history:", "", 1))


def check_gitleaks(repo: Path, report: Report, required: bool) -> None:
    report.touch("secrets")
    exe = shutil.which("gitleaks")
    if not exe:
        report.add("secrets", "FAIL" if required else "WARN", "gitleaks is not installed; only built-in patterns ran")
        return
    with tempfile.TemporaryDirectory() as tmp:
        for mode in ("dir", "git"):
            if mode == "git" and not git(repo, "rev-parse", "--verify", "HEAD").strip():
                continue
            out = Path(tmp) / f"{mode}.json"
            args = [exe, mode, str(repo), "--no-banner", "--report-format", "json", "--report-path", str(out),
                    "--exit-code", "0"]
            p = subprocess.run(args, capture_output=True, text=True)
            if p.returncode != 0:
                report.add("secrets", "FAIL", f"gitleaks {mode} did not run: {p.stderr.strip()[:200]}")
                continue
            try:
                found = json.loads(out.read_text() or "[]")
            except Exception:
                found = []
            for f in found:
                if mode == "dir" and ".git/" in str(f.get("File", "")):
                    continue
                report.add("secrets", "FAIL", f"gitleaks ({mode}) {f.get('RuleID')} in {f.get('File')}:{f.get('StartLine')}")


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check a repository before it goes public.")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST)
    ap.add_argument("--require-denylist", action="store_true",
                    help="fail if the denylist is missing (use locally; CI has no private list)")
    ap.add_argument("--require-gitleaks", action="store_true")
    ap.add_argument("--allow-identity", nargs="*", default=[],
                    help="names / e-mails allowed in commit metadata")
    ap.add_argument("--max-kb", type=int, default=512)
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2
    report = Report()
    deny: list[Entry] = []
    report.touch("denylist")
    if a.denylist.is_file():
        if repo in a.denylist.resolve().parents:
            print("refusing: the denylist is inside the repository and would be published with it", file=sys.stderr)
            return 2
        deny = load_denylist(a.denylist)
        report.add("denylist", "INFO", f"{len(deny)} entries loaded from a list outside the repository")
    else:
        report.add("denylist", "FAIL" if a.require_denylist else "WARN",
                   "no denylist found; only the generic checks ran")
    files = publish_files(repo)
    report.add("files", "INFO", f"{len(files)} files would be published")
    check_files(repo, files, deny, report, a.max_kb)
    check_gitleaks(repo, report, a.require_gitleaks)
    check_identity(repo, deny, a.allow_identity, report)
    check_history(repo, deny, report)
    print(report.render())
    return 1 if report.failed() else 0


if __name__ == "__main__":
    sys.exit(run())
