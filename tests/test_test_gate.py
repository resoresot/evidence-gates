import json
import os
import subprocess
import tempfile
import unittest
import uuid

from helpers import load, run_hook

tg = load("test_gate")


def repo(cfg: dict | None, files: dict[str, str] | None = None) -> str:
    d = tempfile.mkdtemp(prefix="eg-repo-")
    subprocess.run(["git", "init", "-q", d], check=True)
    if cfg is not None:
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "test_gate.json"), "w") as f:
            json.dump(cfg, f)
    for rel, body in (files or {}).items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)
    return d


def stop(cwd: str, session: str):
    return run_hook("test_gate.py", {"cwd": cwd, "session_id": session})


class TestGate(unittest.TestCase):
    def test_no_marker_means_no_gate(self):
        self.assertEqual(stop(repo(None), "s"), (0, None))

    def test_passing_tests_allow_stop(self):
        self.assertEqual(stop(repo({"command": "true"}), "s"), (0, None))

    def test_failing_tests_block_then_give_up(self):
        d, s = repo({"command": "echo boom; false", "max_iterations": 3}), uuid.uuid4().hex
        for attempt in (1, 2):
            code, out = stop(d, s)
            self.assertEqual(out["decision"], "block")
            self.assertIn(f"attempt {attempt}/3", out["reason"])
            self.assertIn("boom", out["reason"])
        code, out = stop(d, s)
        self.assertNotIn("decision", out)
        self.assertIn("failed 3 times", out["systemMessage"])
        # stays exhausted: no new repair loop on the next stop
        code, out = stop(d, s)
        self.assertNotIn("decision", out)

    def test_pass_resets_the_count(self):
        d, s = repo({"command": "test -f ok", "max_iterations": 3}), uuid.uuid4().hex
        self.assertEqual(stop(d, s)[1]["decision"], "block")
        open(os.path.join(d, "ok"), "w").close()
        self.assertEqual(stop(d, s), (0, None))
        os.remove(os.path.join(d, "ok"))
        self.assertIn("attempt 1/3", stop(d, s)[1]["reason"])

    def test_rules_run_only_matching_commands(self):
        cfg = {"rules": [{"match": "api/", "command": "echo api-ran; false"},
                         {"match": "*.html", "command": "echo html-ran; false"}]}
        d = repo(cfg, {"api/x.py": "x"})
        out = stop(d, uuid.uuid4().hex)[1]
        self.assertIn("api-ran", out["reason"])
        self.assertNotIn("html-ran", out["reason"])

    def test_rules_fall_back_to_default(self):
        cfg = {"rules": [{"match": "api/", "command": "false"}], "default": "echo default-ran; false"}
        out = stop(repo(cfg, {"docs/a.md": "x"}), uuid.uuid4().hex)[1]
        self.assertIn("default-ran", out["reason"])

    def test_broken_marker_does_not_block(self):
        d = repo(None)
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "test_gate.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(stop(d, "s"), (0, None))

    def test_match_rule(self):
        self.assertTrue(tg.match_rule("api/", "api/x.py"))
        self.assertTrue(tg.match_rule("*.html", "site/index.html"))
        self.assertTrue(tg.match_rule("config", "app/config.yaml"))
        self.assertFalse(tg.match_rule("api/", "src/api.py"))


if __name__ == "__main__":
    unittest.main()
