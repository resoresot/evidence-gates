"""The context handed to the LLM judge: what it keeps of long commands and long turns."""
import os
import unittest
from unittest import mock

from helpers import load

lj = load("llm_judge")


def bash(cmd):
    return {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}


class CompactAction(unittest.TestCase):
    def test_a_check_at_the_end_of_a_long_command_stays_visible(self):
        # regression: commands were cut at 160 characters, hiding a screenshot check at the end
        cmd = "python3 - <<'EOF'\n" + "x = 1\n" * 400 + "EOF\npython3 shot.py --screenshot out.png"
        out = lj.compact_action(bash(cmd))
        self.assertIn("[runs: screenshot]", out)
        self.assertIn("shot.py", out)  # the tail is kept
        self.assertLess(len(out), 300)

    def test_edits_show_only_the_file_name(self):
        out = lj.compact_action({"type": "tool_use", "name": "Edit", "input": {"file_path": "proj/src/a.py"}})
        self.assertEqual(out, "Edit a.py")

    def test_skipped_count_is_exact(self):
        items = [bash(f"echo {i}") for i in range(40)]
        acts = lj.compact_actions(items)
        self.assertEqual(len(acts), lj.MAX_ACTIONS)
        kept = len(acts) - 1
        self.assertIn(f"... {40 - kept} more actions ...", acts)


class CleanEnv(unittest.TestCase):
    def test_drops_session_state_but_keeps_how_to_reach_the_model(self):
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_USE_BEDROCK": "1",
               "CLAUDE_CODE_OAUTH_TOKEN": "t", "PATH": "/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = lj.clean_env()
        self.assertNotIn("CLAUDECODE", out)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", out)
        self.assertEqual(out["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(out["CLAUDE_CODE_OAUTH_TOKEN"], "t")
        self.assertEqual(out["EVIDENCE_GATES_DISABLE"], "all")


if __name__ == "__main__":
    unittest.main()
