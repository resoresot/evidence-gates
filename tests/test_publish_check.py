"""The leak checker is tested against a repository with leaks planted in it.

Planted values are assembled at run time (never written literally in this
file), so this test file itself stays clean under the checker.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import publish_check as pc  # noqa: E402

J = "".join  # assemble planted values so they never appear literally here
FAKE_EMAIL = J(["taro", ".", "example", "@", "mailbox", ".", "io"])
FAKE_HOME = J(["/", "Users", "/", "taro", "yamada", "/notes"])
FAKE_KEY = J(["sk-", "ant-", "api03-", "A" * 40])
FAKE_PHONE = J(["090", "-", "1234", "-", "5678"])
SECRET_PROJECT = J(["moon", "harbor"])
BIDI_CHAR = chr(0x202E)


def make_repo(files: dict[str, str], author: tuple[str, str] = ("dev", "dev@users.noreply.github.com")) -> Path:
    d = Path(tempfile.mkdtemp(prefix="eg-pub-"))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", author[0]], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", author[1]], check=True)
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def commit(d: Path, msg: str = "c") -> None:
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", msg], check=True)


def denylist(*lines: str) -> Path:
    fd, p = tempfile.mkstemp(suffix=".txt", prefix="eg-deny-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return Path(p)


def check(repo: Path, *args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = pc.run([str(repo), *args])
    return code, buf.getvalue()


class PublishCheck(unittest.TestCase):
    def test_clean_repo_passes(self):
        d = make_repo({"README.md": "# demo\nSee /Users/you/project for an example.\n", "a.py": "print(1)\n"})
        commit(d)
        code, out = check(d, "--denylist", str(denylist(SECRET_PROJECT)))
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: PASS", out)

    def test_reserved_example_domains_are_not_personal(self):
        reserved = J(["demo", "@", "shop", ".", "test"]) + " " + J(["a", "@", "b", ".", "example"])
        d = make_repo({"a.md": f"{reserved}\n"})
        self.assertEqual(check(d, "--denylist", str(denylist("zzz")))[0], 0)

    def test_each_kind_of_leak_is_found(self):
        d = make_repo({
            "notes.md": f"contact {FAKE_EMAIL}\npath {FAKE_HOME}\ncall {FAKE_PHONE}\n",
            "config.py": f"KEY = '{FAKE_KEY}'\n",
            "src/app.py": f"# ported from the {SECRET_PROJECT} pipeline\n",
            "trick.py": f"access = 'user' {BIDI_CHAR} # admin\n",
            ".env": "X=1\n",
        })
        code, out = check(d, "--denylist", str(denylist(SECRET_PROJECT)))
        self.assertEqual(code, 1)
        for expected in ("e-mail address", "home-directory path", "phone number", "possible anthropic key",
                         "src/app.py:1 matches denylist", "bidirectional control character",
                         ".env is a kind of file"):
            self.assertIn(expected, out)

    def test_denylist_terms_are_never_printed(self):
        d = make_repo({"a.md": f"{SECRET_PROJECT}\n"})
        _, out = check(d, "--denylist", str(denylist(SECRET_PROJECT)))
        self.assertNotIn(SECRET_PROJECT, out)
        self.assertIn(pc.mask(SECRET_PROJECT), out)
        self.assertTrue(pc.mask(SECRET_PROJECT).startswith("m*"))

    def test_regex_and_warn_entries(self):
        d = make_repo({"a.md": "see TKT-4821 and also kettle\n"})
        code, out = check(d, "--denylist", str(denylist(r"re:\bTKT-\d{4}\b", "warn:kettle")))
        self.assertEqual(code, 1)
        self.assertIn("regex #1", out)
        self.assertIn("WARN: a.md:1 matches denylist #2", out)

    def test_leak_removed_later_is_still_found_in_history(self):
        d = make_repo({"a.md": f"{SECRET_PROJECT}\n"})
        commit(d, "first")
        (d / "a.md").write_text("clean\n", encoding="utf-8")
        commit(d, "remove it")
        code, out = check(d, "--denylist", str(denylist(SECRET_PROJECT)))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] history", out)
        self.assertIn("[PASS] denylist", out)  # the current files are clean

    def test_commit_identity(self):
        brand = J(["Moon", " Harbor", " Lab"])
        d = make_repo({"a.md": "x\n"}, author=(brand, J(["owner", "@", "mailbox", ".", "io"])))
        commit(d)
        code, out = check(d, "--denylist", str(denylist("moon harbor")))
        self.assertEqual(code, 1)
        self.assertIn("commit author matches denylist", out)
        self.assertIn("is not a no-reply address", out)

    def test_untracked_but_ignored_files_are_not_published(self):
        d = make_repo({".gitignore": "private/\n", "private/notes.md": f"{SECRET_PROJECT}\n", "a.md": "ok\n"})
        code, out = check(d, "--denylist", str(denylist(SECRET_PROJECT)))
        self.assertEqual(code, 0, out)

    def test_denylist_inside_repo_is_refused(self):
        d = make_repo({"a.md": "x\n"})
        inside = d / "deny.txt"
        inside.write_text("x\n", encoding="utf-8")
        code, _ = check(d, "--denylist", str(inside))
        self.assertEqual(code, 2)

    def test_missing_denylist(self):
        d = make_repo({"a.md": "x\n"})
        missing = str(Path(tempfile.mkdtemp()) / "none.txt")
        self.assertEqual(check(d, "--denylist", missing)[0], 0)
        self.assertEqual(check(d, "--denylist", missing, "--require-denylist")[0], 1)

    def test_oversized_file(self):
        d = make_repo({"big.txt": "x" * 3000})
        code, out = check(d, "--denylist", str(denylist("zzz")), "--max-kb", "1")
        self.assertEqual(code, 1)
        self.assertIn("big.txt is 2 KB", out)


if __name__ == "__main__":
    unittest.main()
