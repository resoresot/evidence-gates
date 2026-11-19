import unittest

from helpers import load

sw = load("shell_writes")


class ShellWrites(unittest.TestCase):
    def test_writes(self):
        for cmd in (
            "printf hi > out.txt", "echo a >> notes.md", "ls | tee list.txt", "sed -i '' s/a/b/ f.py",
            "perl -pi -e s/a/b/ f.py", "cp a.py b.py", "mv a b", "cd x && echo hi > y.txt", "X=1 cp a b",
            # inline scripts: the form agents use most when they skip the Edit tool
            "python3 - <<'EOF'\nimport pathlib\np=pathlib.Path('a.py'); p.write_text(p.read_text().replace('a','b'))\nEOF",
            "cd repo && python3 - <<'EOF'\nopen('x.py','w').write('x')\nEOF",
            "python3 -c \"open('out.txt', mode='w').write('1')\"",
            "/usr/local/bin/python3 -c 'import shutil; shutil.copy(\"a\", \"b\")'",
            "node -e \"require('fs').writeFileSync('a.json','{}')\"",
            "python - <<EOF\nimport os\nos.replace('a','b')\nEOF",
            "python3 - <<'EOF'\nfrom pathlib import Path\nwith Path('n.md').open('a') as f: f.write('x')\nEOF",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(sw.command_writes(cmd))

    def test_not_writes(self):
        for cmd in (
            "ls > /dev/null", "make 2>&1", "cmd 2>/dev/null", "echo hi > /tmp/x", "echo 'a > b'",
            "grep -r cp .", "sed s/a/b/ f.py", "cat a.txt", "ls | tee", "cp a /private/tmp/b", "cmd >&2",
            "python3 -c \"print(open('a.txt').read())\"",
            "python3 - <<'EOF'\nimport json\nprint(json.load(open('a.json')))\nEOF",
            "python3 -c \"s='a'.replace('a','b'); print(s)\"",  # str.replace is not a file write
            "python3 script.py", "python3 -m pytest -q", "grep write_text src/*.py",
            "node -e \"console.log(require('fs').readFileSync('a'))\"",
            "", "echo 'unterminated",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(sw.command_writes(cmd))


class WrittenPaths(unittest.TestCase):
    def test_targets(self):
        cases = {
            "echo note >> NOTES.md": ["NOTES.md"],
            "printf x > a.py": ["a.py"],
            "cp a.py a.py.bak": ["a.py.bak"],
            "sed -i '' s/a/b/ docs/x.md": ["docs/x.md"],
            "perl -pi -e s/a/b/ notes.txt": ["notes.txt"],
            "python3 - <<'EOF'\nimport pathlib\npathlib.Path('report.md').write_text('x')\nEOF": ["report.md"],
            "ls": [],
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(sw.written_paths(cmd), want)

    def test_unknown_targets_are_none(self):
        for cmd in ("echo x > $OUT/notes.md", "python3 - <<EOF\np=open(f,'w')\nEOF", "sed -i s/a/b/ Makefile"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(sw.written_paths(cmd))


if __name__ == "__main__":
    unittest.main()
