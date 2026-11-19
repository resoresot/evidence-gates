"""Tests for the measurement tools: replay.py (offline) and stats.py (live log)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "gates"))
import replay  # noqa: E402
import stats  # noqa: E402

TS = "2026-01-05T10:00:00Z"


def user(text, ts=TS):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def assistant(*items, ts=TS):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": list(items)}}


def bash(cid, cmd):
    return {"type": "tool_use", "id": cid, "name": "Bash", "input": {"command": cmd}}


def result(cid, text, ts=TS):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": cid, "content": text}]}}


def say(t):
    return {"type": "text", "text": t}


def write_session(rows) -> Path:
    d = Path(tempfile.mkdtemp(prefix="eg-replay-")) / "proj"
    d.mkdir()
    with open(d / "s.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return d.parent


def run_replay(root: Path, *args) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        replay.main(["--dir", str(root), "--json", *args])
    return json.loads(buf.getvalue())


class Replay(unittest.TestCase):
    def test_zsh_precision_and_recall(self):
        root = write_session([
            user("search"),
            assistant(bash("a", "grep -r --include=*.py TODO .")), result("a", "zsh: no matches found: --include=*.py"),
            assistant(bash("b", "find . -name *.md")), result("b", "./README.md"),          # blocked, did not fail
            assistant(bash("c", "ls dir/*.txt | grep x")), result("c", "zsh: no matches found: dir/*.txt"),  # missed
            assistant(bash("d", "echo ok")), result("d", "ok"),
            assistant(bash("e", "rg -g *.ts foo")), result("e", "PreToolUse:Bash hook error: blocked"),  # live hook: excluded
            assistant(say("done looking")),
        ])
        z = run_replay(root)["zsh_glob"]
        self.assertEqual(z["bash_calls"], 4)
        self.assertEqual((z["would_block"], z["would_block_and_failed"]), (2, 1))
        self.assertEqual((z["nomatch_failures"], z["nomatch_caught"]), (2, 1))

    def test_done_verified_vs_unverified_and_next_message(self):
        edit = {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": "a.py"}}
        root = write_session([
            user("fix it"), assistant(edit), result("e", "ok"), assistant(say("直しました。")),
            user("まだ動かない"),
            assistant(edit), result("e", "ok"), assistant(bash("t", "python3 -m pytest -q")), result("t", "1 passed"),
            assistant(say("直しました。")),
            user("ありがとう"), assistant(say("どういたしまして")),
        ])
        t = run_replay(root)["completion"]
        self.assertEqual((t["done_unverified"], t["done_unverified_complaint"]), (1, 1))
        self.assertEqual((t["done_verified"], t["done_verified_complaint"]), (1, 0))

    def test_hook_feedback_does_not_start_a_turn(self):
        root = write_session([
            user("go"), assistant(say("進めてよいですか？")),
            user("Stop hook feedback: decide yourself"), assistant(say("進めました。")),
            user("はい"),
        ])
        t = run_replay(root)["completion"]
        self.assertEqual(t["turns"], 1)  # the feedback row is not a user turn

    def test_window(self):
        root = write_session([user("x", ts="2025-12-01T00:00:00Z"), assistant(bash("a", "echo 1")),
                              result("a", "1", ts="2025-12-01T00:00:00Z")])
        self.assertEqual(run_replay(root, "--after", "2026-01-01")["zsh_glob"].get("bash_calls", 0), 0)
        self.assertEqual(run_replay(root, "--before", "2026-01-01")["zsh_glob"]["bash_calls"], 1)


class Stats(unittest.TestCase):
    def test_weekly_fire_rate(self):
        rows = ([{"ts": "2026-01-05T10:00:00", "gate": "completion", "decision": "pass"}] * 3
                + [{"ts": "2026-01-06T10:00:00", "gate": "completion", "decision": "block", "detail": "done,ask"}]
                + [{"ts": "2026-01-07T10:00:00", "gate": "zsh_glob", "decision": "unsure", "detail": "eval"}])
        out = "\n".join(stats.summarize(rows, 8))
        self.assertIn("| 2026-01-05 | completion | 1 | 3 | 25.0% | ask:1 done:1 |", out)
        self.assertIn("| 2026-01-05 | zsh_glob | 0 | 0 | - | unsure 1 |", out)


if __name__ == "__main__":
    unittest.main()
