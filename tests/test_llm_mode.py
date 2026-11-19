"""The opt-in LLM mode of completion_gate, with a fake `claude` binary standing in for the model."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import run_hook, text, tool, turn, write_transcript

JA = "これを直して"
FAKE = """#!/bin/sh
echo called >> "$FAKE_CALLS"
if [ "$FAKE_MODE" = "fail" ]; then echo "boom" >&2; exit 1; fi
printf '{"structured_output": {"done_unverified": %s, "unneeded_question": %s}, "total_cost_usd": 0.001, "duration_api_ms": 900}' "$FAKE_DONE" "$FAKE_ASK"
"""


class LLMMode(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="eg-llm-"))
        self.fake = self.dir / "claude"
        self.fake.write_text(FAKE)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.calls = self.dir / "calls.txt"

    def stop(self, rows, done="false", ask="false", mode="ok", done_rule="shadow"):
        env = {"EVIDENCE_GATES_JUDGE": "sonnet", "CLAUDE_BIN": str(self.fake), "FAKE_CALLS": str(self.calls),
               "FAKE_DONE": done, "FAKE_ASK": ask, "FAKE_MODE": mode, "EVIDENCE_GATES_DONE_RULE": done_rule}
        return run_hook("completion_gate.py", {"transcript_path": write_transcript(rows), "session_id": "s"}, env=env)[1]

    def n_calls(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_llm_blocks_what_it_judges_unverified(self):
        rows = turn(JA, tool("Edit", file_path="/a.py"), tool("Bash", command="git status"), text("直しました。"))
        out = self.stop(rows, done="true")
        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.n_calls(), 1)

    def test_llm_can_overrule_the_rule(self):
        # the rule would block this (only a look-only command after the edit); the LLM says it is fine
        rows = turn(JA, tool("Edit", file_path="/a.py"), tool("Bash", command="git status"), text("直しました。"))
        self.assertIsNone(self.stop(rows, done="false"))

    def test_model_failure_falls_back_to_the_rule(self):
        rows = turn(JA, tool("Edit", file_path="/a.py"), text("直しました。"))
        self.assertIsNone(self.stop(rows, mode="fail"))  # the rule's verdict, which is shadow by default
        out = self.stop(rows, mode="fail", done_rule="block")
        self.assertEqual(out["decision"], "block")

    def test_the_call_is_recorded_in_the_events_log(self):
        import json
        import tempfile
        from pathlib import Path
        log = tempfile.mkdtemp(prefix="eg-log-")
        rows = turn(JA, tool("Edit", file_path="/a.py"), text("直しました。"))
        env = {"EVIDENCE_GATES_JUDGE": "sonnet", "CLAUDE_BIN": str(self.fake), "FAKE_CALLS": str(self.calls),
               "FAKE_DONE": "false", "FAKE_ASK": "false", "FAKE_MODE": "fail", "EVIDENCE_GATES_LOG_DIR": log}
        run_hook("completion_gate.py", {"transcript_path": write_transcript(rows), "session_id": "s"}, env=env)
        env["FAKE_MODE"] = "ok"
        run_hook("completion_gate.py", {"transcript_path": write_transcript(rows), "session_id": "s"}, env=env)
        details = [json.loads(x)["detail"] for x in Path(log, "events.jsonl").read_text().splitlines()]
        self.assertIn("judge=failed", details[0])  # a judge that silently never runs would show here
        self.assertIn("judge=ok", details[1])
        self.assertIn("$0.0010", details[1])

    def test_prefilter_skips_turns_without_a_claim_or_question(self):
        rows = turn(JA, tool("Read", file_path="/a.py"), text("読みました。設定は既定のままです。"))
        self.assertIsNone(self.stop(rows, done="true", ask="true"))
        self.assertEqual(self.n_calls(), 0)

    def test_reason_tag_still_lets_a_real_question_through(self):
        rows = turn(JA, text("本番に反映してよいですか？\n確認理由: 不可逆"))
        self.assertIsNone(self.stop(rows, ask="true"))


if __name__ == "__main__":
    unittest.main()
