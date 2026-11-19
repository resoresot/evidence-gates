import unittest

from helpers import load, run_hook

zg = load("zsh_glob_gate")


class Decide(unittest.TestCase):
    def test_blocked(self):
        for cmd, rule in (
            ("grep -r --include=*.py TODO .", "prefix"),
            ("grep -rn foo --exclude-dir=*.git .", "prefix"),
            ("find . -name *.md", "find"),
            ("find src -iname *.TS -type f", "find"),
            ("rg -g *.ts foo", "rg"),
            ("rg -g*.ts foo", "rg"),
            ("rg --glob *.ts foo", "rg"),
            ("cd x && grep -r --include=*.py a .", "prefix"),
            ("echo --x=*", "prefix"),  # zsh aborts this too: no file name starts with --
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(zg.decide(cmd, "zsh")[:2], ("block", rule))

    def test_passed(self):
        for cmd in (
            "grep -r --include='*.py' TODO .", 'find . -name "*.md"', "rg -g '*.ts' foo",
            "ls *.md", "echo '--x=*'", "grep -r TODO .", "noglob find . -name *.md",
            "python3 - <<'EOF'\nprint('--include=*.py')\nEOF",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(zg.decide(cmd, "zsh")[0], "pass", cmd)

    def test_bash_and_nonomatch_only_unsure_for_prefix_form(self):
        self.assertEqual(zg.decide("grep -r --include=*.py a .", "bash")[0], "unsure")
        self.assertEqual(zg.decide("setopt nonomatch; grep -r --include=*.py a .", "zsh")[0], "unsure")
        # find/rg forms still expand on a match in bash
        self.assertEqual(zg.decide("find . -name *.md", "bash")[0], "block")

    def test_patterns_that_zsh_matches_instead_of_expanding(self):
        # found in review: case patterns and [[ ]] were blocked although zsh does not expand them
        for cmd in ('case "$1" in --name=*) echo;; esac', 'case "$1" in\n  --name=*) echo hi;;\nesac',
                    "[[ $x == --foo=* ]] && echo y"):
            with self.subTest(cmd=cmd):
                self.assertEqual(zg.decide(cmd, "zsh")[0], "unsure")

    def test_uncertain_word_boundaries_are_unsure_not_blocked(self):
        for cmd in ("grep $(echo --include=*.py) a", "eval grep --include=*.py a", "grep \"--include=*.py a"):
            with self.subTest(cmd=cmd):
                self.assertEqual(zg.decide(cmd, "zsh")[0], "unsure")


class Process(unittest.TestCase):
    def test_deny_message(self):
        code, out = run_hook("zsh_glob_gate.py", {"tool_name": "Bash", "tool_input": {"command": "find . -name *.md"}},
                             env={"SHELL": "/bin/zsh"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("not quoted", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_other_tools_and_garbage_pass(self):
        self.assertEqual(run_hook("zsh_glob_gate.py", {"tool_name": "Read", "tool_input": {}}), (0, None))
        self.assertEqual(run_hook("zsh_glob_gate.py", {"tool_name": "Bash", "tool_input": {"command": 1}}), (0, None))

    def test_codex_style_argv(self):
        ev = {"tool_name": "Bash", "tool_input": {"command": ["/bin/zsh", "-lc", "rg -g *.ts foo"]}}
        self.assertEqual(run_hook("zsh_glob_gate.py", ev)[1]["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
