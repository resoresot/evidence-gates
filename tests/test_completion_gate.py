import unittest

from helpers import load, run_hook, text, tool, turn, write_transcript

cg = load("completion_gate")
JA = "これを直して"
PY_EDIT = "python3 - <<'EOF'\nimport pathlib\npathlib.Path('a.py').write_text('x')\nEOF"


def checks(rows, done_rule="block", **extra):
    # the done rule's logic is tested with blocking on; its default (shadow) is tested in Process
    event = {"transcript_path": write_transcript(rows), **extra}
    return cg.evaluate(event, done_rule=done_rule)


class Ask(unittest.TestCase):
    def test_permission_questions_block(self):
        for reply in ("①から始めていいですか？", "結果は以上です。\n\nこの方針で②に進めますか？",
                      "どちらにしますか?", "Done with the analysis. Want me to apply it?"):
            with self.subTest(reply=reply):
                self.assertIn("ask", checks(turn(JA, text(reply))))

    def test_reason_tag_lets_a_real_question_through(self):
        for reply in ("送信してよいですか？\n確認理由: 外部送信", "Which color?\nReason to ask: preference"):
            with self.subTest(reply=reply):
                self.assertNotIn("ask", checks(turn(JA, text(reply))))

    def test_unknown_reason_does_not_pass(self):
        self.assertIn("ask", checks(turn(JA, text("進めますか？\n確認理由: なんとなく"))))

    def test_not_questions(self):
        for reply in ("なぜ止まったのか？ 原因は設定でした。\n\n直して確認済みです。",
                      "出力:\n```\nOK to proceed?\n```\n以上で完了しています。",
                      "ログを調べました。原因は設定でした。",
                      "設定が決まったら、画面をそれに合わせて作り直します。",
                      "今後:\n1. ログを集めます。\n2. 遅ければ設定を見直します。"):
            with self.subTest(reply=reply):
                self.assertNotIn("ask", checks(turn(JA, text(reply))))

    def test_announcing_a_check_without_doing_it(self):
        for reply in ("原因はおそらく設定です。確認します。", "ログを調べます。", "I'll check the logs."):
            with self.subTest(reply=reply):
                self.assertIn("ask", checks(turn(JA, text(reply))))


class Done(unittest.TestCase):
    def test_edit_then_claim_without_check(self):
        self.assertIn("done", checks(turn(JA, tool("Edit", file_path="/a.py"), text("直しました。"))))

    def test_check_after_edit_passes(self):
        self.assertNotIn("done", checks(turn(JA, tool("Edit", file_path="/a.py"), tool("Bash", command="pytest"),
                                             text("直しました。"))))

    def test_check_before_edit_does_not_count(self):
        self.assertIn("done", checks(turn(JA, tool("Bash", command="pytest"), tool("Write", file_path="/a.py"),
                                          text("直しました。"))))

    def test_browser_tool_counts_as_check(self):
        self.assertNotIn("done", checks(turn(JA, tool("Edit", file_path="/a.html"),
                                             tool("mcp__browser__screenshot"), text("直しました。"))))

    def test_honest_unverified_passes(self):
        self.assertNotIn("done", checks(turn(JA, tool("Edit", file_path="/a.py"),
                                             text("直しました(未検証: CI で確かめてください)。"))))

    def test_no_edit_no_check_needed(self):
        self.assertNotIn("done", checks(turn(JA, tool("Read", file_path="/a.py"), text("できました。"))))

    def test_shell_edits_count(self):
        self.assertIn("done", checks(turn(JA, tool("Bash", command=PY_EDIT), text("直しました。"))))
        self.assertIn("done", checks(turn(JA, tool("Bash", command="echo x > a.py"), text("反映しました。"))))
        self.assertNotIn("done", checks(turn(JA, tool("Bash", command=PY_EDIT),
                                             tool("Bash", command="python3 -m pytest -q"), text("直しました。"))))

    def test_write_and_test_in_one_command_passes(self):
        self.assertNotIn("done", checks(turn(JA, tool("Bash", command=PY_EDIT + " && python3 -m pytest -q"),
                                             text("直しました。"))))

    def test_look_only_commands_are_not_a_check(self):
        # found in review: `ls` or `git status` after an edit used to count as verification
        for cmd in ("git status", "ls -la", "cd src && git diff", "cat a.py | head"):
            with self.subTest(cmd=cmd):
                self.assertIn("done", checks(turn(JA, tool("Edit", file_path="/a.py"), tool("Bash", command=cmd),
                                                  text("直しました。"))))
        self.assertNotIn("done", checks(turn(JA, tool("Edit", file_path="/a.py"),
                                             tool("Bash", command="curl -s localhost:8000/health"), text("直しました。"))))

    def test_claim_phrases(self):
        # found in review: "not done yet" was read as a claim, 「修正済み」「終わりました」 were missed
        edit = tool("Edit", file_path="/a.py")
        self.assertNotIn("done", checks(turn(JA, edit, text("It is not done yet; next I update the tests."))))
        self.assertNotIn("done", checks(turn(JA, edit, text("The bug is not fixed yet; the parser still fails."))))
        for reply in ("修正済みです。", "終わりました。", "Done. The parser handles both formats."):
            with self.subTest(reply=reply):
                self.assertIn("done", checks(turn(JA, edit, text(reply))))

    def test_prose_edits_need_no_check(self):
        # error analysis on real transcripts: most wrong blocks came from a note or report
        # written after a verified code change
        for last in (tool("Edit", file_path="/docs/notes.md"), tool("Bash", command="echo done >> NOTES.md"),
                     tool("Bash", command="cp app.py app.py.bak")):
            with self.subTest(last=last):
                self.assertNotIn("done", checks(turn(JA, tool("Edit", file_path="/a.py"), tool("Bash", command="pytest"),
                                                     last, text("直しました。"))))
        # but code written through the shell, or to an unknown target, still needs a check
        for last in (tool("Bash", command="printf x > a.py"), tool("Bash", command="echo x > $OUT/a")):
            with self.subTest(last=last):
                self.assertIn("done", checks(turn(JA, tool("Bash", command="pytest"), last, text("直しました。"))))

    def test_read_only_shell_is_not_an_edit(self):
        self.assertNotIn("done", checks(turn(JA, tool("Bash", command="python3 -c \"print(open('a').read())\""),
                                             text("できました。"))))

    def test_earlier_turn_edits_do_not_count(self):
        rows = turn(JA, tool("Edit", file_path="/a.py"), text("直しました。")) + turn("ありがとう", text("できました。"))
        self.assertNotIn("done", checks(rows))


class Lang(unittest.TestCase):
    def test_english_reply_to_japanese_user(self):
        reply = "I updated the parser and the tests. " * 12
        self.assertIn("lang", checks(turn(JA, text(reply))))

    def test_short_or_mixed_or_json_passes(self):
        for reply in ("OK", "テストは通りました。`pytest -q` の結果は 12 passed でした。", '{"status": "ok", ' + '"k": "v", ' * 60 + "}"):
            with self.subTest(reply=reply[:20]):
                self.assertNotIn("lang", checks(turn(JA, text(reply))))

    def test_english_user_is_not_checked(self):
        self.assertNotIn("lang", checks(turn("fix it", text("I updated the parser. " * 20))))


class Process(unittest.TestCase):
    def test_blocks_once_with_reason(self):
        path = write_transcript(turn(JA, tool("Edit", file_path="/a.py"), text("直しました。")))
        code, out = run_hook("completion_gate.py", {"transcript_path": path, "session_id": "s"},
                             env={"EVIDENCE_GATES_DONE_RULE": "block"})
        self.assertEqual(code, 0)
        self.assertEqual(out["decision"], "block")
        self.assertIn("nothing was run", out["reason"])

    def test_done_rule_is_shadow_by_default(self):
        import json
        import tempfile
        from pathlib import Path
        path = write_transcript(turn(JA, tool("Edit", file_path="/a.py"), text("直しました。")))
        log = tempfile.mkdtemp(prefix="eg-log-")
        code, out = run_hook("completion_gate.py", {"transcript_path": path, "session_id": "s"},
                             env={"EVIDENCE_GATES_LOG_DIR": log})
        self.assertEqual((code, out), (0, None))  # not blocked
        rows = [json.loads(x) for x in Path(log, "events.jsonl").read_text().splitlines()]
        self.assertEqual((rows[-1]["decision"], rows[-1]["detail"]), ("pass", "done_shadow"))  # but counted

    def test_shadow_done_does_not_hide_a_blocking_check(self):
        path = write_transcript(turn(JA, tool("Edit", file_path="/a.py"), text("直しました。進めてよいですか？")))
        code, out = run_hook("completion_gate.py", {"transcript_path": path, "session_id": "s"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("asking permission", out["reason"])
        self.assertNotIn("nothing was run", out["reason"])

    def test_never_blocks_twice_in_a_row(self):
        path = write_transcript(turn(JA, tool("Edit", file_path="/a.py"), text("直しました。")))
        self.assertEqual(run_hook("completion_gate.py", {"transcript_path": path, "stop_hook_active": True}),
                         (0, None))

    def test_kill_switches(self):
        path = write_transcript(turn(JA, tool("Edit", file_path="/a.py"), text("直しました。")))
        ev = {"transcript_path": path}
        self.assertEqual(run_hook("completion_gate.py", ev, env={"EVIDENCE_GATES_DISABLE": "completion"}), (0, None))
        self.assertEqual(run_hook("completion_gate.py", ev, env={"EVIDENCE_GATES_SKIP": "done",
                                                                 "EVIDENCE_GATES_DONE_RULE": "block"}), (0, None))

    def test_fail_open_on_garbage(self):
        import subprocess
        import sys
        from helpers import GATES
        p = subprocess.run([sys.executable, str(GATES / "completion_gate.py")], input="not json",
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
