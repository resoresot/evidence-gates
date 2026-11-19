import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import GATES, load

ls = load("lessons")


class Lessons(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-lessons-")
        self.env = {**os.environ, "EVIDENCE_GATES_LESSONS": os.path.join(self.dir, "lessons.jsonl"),
                    "EVIDENCE_GATES_LOG_DIR": self.dir}
        self.cwd = tempfile.mkdtemp(prefix="eg-proj-")

    def cli(self, *args, stdin=None):
        p = subprocess.run([sys.executable, str(GATES / "lessons.py"), *args], input=stdin, capture_output=True,
                           text=True, env=self.env, cwd=self.cwd)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def add_glob_lesson(self):
        return self.cli("add", "--when", "searching files with grep --include or find -name in zsh",
                        "--do", "quote the glob pattern", "--why", "an unquoted glob aborted a search",
                        "--tags", "zsh,glob,grep").strip()

    def test_add_recur_promote_report(self):
        lid = self.add_glob_lesson()
        self.assertEqual(lid, "L001")
        self.assertEqual(self.cli("add", "--when", "x y z", "--do", "d", "--why", "w").strip(), "L002")
        self.cli("recur", lid, "--note", "again", "--at", "2026-01-02")
        self.cli("recur", lid)
        report = self.cli("report", "--min", "2")
        self.assertIn("| L001 | 2 |", report)
        self.assertIn("candidate for a gate", report)
        self.cli("promote", lid, "--gate", "zsh_glob_gate")
        self.assertIn("gate: zsh_glob_gate", self.cli("report"))
        with open(self.env["EVIDENCE_GATES_LESSONS"], encoding="utf-8") as f:
            stored = [json.loads(x) for x in f]
        self.assertEqual(stored[0]["recurrences"][0], {"at": "2026-01-02", "note": "again"})

    def test_hook_injects_relevant_lessons_only(self):
        self.add_glob_lesson()
        self.cli("add", "--when", "writing release notes", "--do", "list breaking changes first", "--why", "w")
        out = self.cli("hook", stdin=json.dumps({"prompt": "find all TODO with grep --include in the zsh shell",
                                                 "cwd": self.cwd, "session_id": "s"}))
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("L001", ctx)
        self.assertNotIn("L002", ctx)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "events.jsonl")))

    def test_hook_skips_promoted_and_unrelated(self):
        lid = self.add_glob_lesson()
        self.cli("promote", lid, "--gate", "zsh_glob_gate")
        self.assertEqual(self.cli("hook", stdin=json.dumps({"prompt": "grep --include in zsh", "cwd": self.cwd})), "")
        self.assertEqual(self.cli("hook", stdin=json.dumps({"prompt": "hello", "cwd": self.cwd})), "")
        self.assertEqual(self.cli("hook", stdin="not json"), "")

    def test_project_store_and_japanese_matching(self):
        self.cli("add", "--project", "--when", "請求書の消費税を計算するとき", "--do", "税率ごとに1回だけ丸める",
                 "--why", "行ごとに丸めて合計がずれた")
        self.assertTrue(os.path.exists(os.path.join(self.cwd, ".claude", "lessons.jsonl")))
        out = self.cli("hook", stdin=json.dumps({"prompt": "請求書の消費税の計算を直して", "cwd": self.cwd}))
        self.assertIn("税率ごとに1回だけ丸める", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_particles_alone_do_not_match(self):
        # found in review: an invoice lesson matched a login-screen request through する/とき/ると
        self.cli("add", "--when", "請求書の消費税を計算するとき", "--do", "税率ごとに1回だけ丸める", "--why", "w")
        out = self.cli("hook", stdin=json.dumps({"prompt": "ログイン画面を実装するときのテストを書いて", "cwd": self.cwd}))
        self.assertEqual(out, "")

    def test_tokens(self):
        self.assertIn("grep", ls.tokens("use grep here"))
        self.assertIn("請求", ls.tokens("請求書"))
        self.assertNotIn("the", ls.tokens("the"))


if __name__ == "__main__":
    unittest.main()
