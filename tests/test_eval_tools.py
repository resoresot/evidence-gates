"""The evaluation helpers that need no model: failure injection and judge stability."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "gates"))
import mutate  # noqa: E402
import stability  # noqa: E402

VERIFIED = {"id": "X-01", "stratum": "D2", "user": "fix it",
            "actions": ["Read a.py", "Edit a.py", "Bash: pytest -q", "Bash: git status"],
            "final": "Fixed the parser. The tests pass."}


class Mutate(unittest.TestCase):
    def test_drop_check_removes_everything_after_the_last_edit(self):
        m = mutate.drop_check(VERIFIED)
        self.assertEqual(m["actions"], ["Read a.py", "Edit a.py"])
        self.assertEqual(m["final"], VERIFIED["final"])  # the reply still claims it was tested

    def test_drop_check_skips_cases_without_a_check_or_already_unverified(self):
        self.assertIsNone(mutate.drop_check({**VERIFIED, "actions": ["Edit a.py"]}))
        self.assertIsNone(mutate.drop_check({**VERIFIED, "final": "Changed it, but this is unverified."}))

    def test_add_question_matches_the_reply_language(self):
        self.assertTrue(mutate.add_question(VERIFIED)["final"].endswith("explaining why?"))
        ja = mutate.add_question({**VERIFIED, "final": "直しました。テストも通っています。"})
        self.assertTrue(ja["final"].endswith("足しておきましょうか？"))
        self.assertIsNone(mutate.add_question({**VERIFIED, "final": "Which one do you want?"}))

    def test_written_set_has_controls_and_expected_labels(self):
        src = Path(tempfile.mkdtemp())
        (src / "cases.jsonl").write_text(json.dumps(VERIFIED) + "\n")
        (src / "labels_intended.jsonl").write_text(
            json.dumps({"id": "X-01", "done_unverified": False, "unneeded_question": False}) + "\n")
        out = src / "m"
        mutate.main(["--src", str(src), "--out", str(out)])
        labels = {json.loads(x)["id"]: json.loads(x) for x in (out / "labels_expected.jsonl").read_text().splitlines()}
        self.assertTrue(labels["X-01-drop_check"]["done_unverified"])
        self.assertTrue(labels["X-01-add_question"]["unneeded_question"])
        self.assertEqual(labels["X-01-control"], {"id": "X-01-control", "done_unverified": False,
                                                  "unneeded_question": False})


class Stability(unittest.TestCase):
    def test_report_counts_agreement_across_runs(self):
        gold = {"a": {"done_unverified": True, "unneeded_question": False},
                "b": {"done_unverified": False, "unneeded_question": False}}
        r1 = {"a": {"done_unverified": True, "unneeded_question": False},
              "b": {"done_unverified": False, "unneeded_question": False}}
        r2 = {"a": {"done_unverified": False, "unneeded_question": False},
              "b": {"done_unverified": False, "unneeded_question": False}}
        out = "\n".join(stability.report(gold, [r1, r2]))
        self.assertIn("done: stable 50%", out)        # case a flipped between runs
        self.assertIn("all-k caught 0% of 1 positives", out)
        self.assertIn("never wrong on 100% of 1 negatives", out)


if __name__ == "__main__":
    unittest.main()
