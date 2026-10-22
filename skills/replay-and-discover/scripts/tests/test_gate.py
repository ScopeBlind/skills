"""How a shell command is joined, what it writes, and what a test runner said. Run: python3 -m unittest discover -s scripts/tests"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from sbreplay import classify  # noqa: E402
from sbreplay.verdict import test_verdict  # noqa: E402


def gate(command, first, then=None):
    segs = classify.plan(command)
    idx = lambda kind: next(i for i, s in enumerate(segs) if kind in s.steps)  # noqa: E731
    return classify.gated(segs, idx(first), idx(then) if then else None)


class GatedTest(unittest.TestCase):
    def test_a_failure_stops_the_push(self):
        for cmd in ("npm test && git push origin x",
                    "cd app && npm test && npm run build && git push",
                    "set -e; npm test; git push",
                    "set -euo pipefail\nnpm test | tail -5\ngit push",
                    "set -o pipefail; npm test 2>&1 | tail -5 && git push",
                    "npm test &&\n  git push",
                    "(npm test) && git push",
                    "npm test \\\n  && git push"):
            self.assertTrue(gate(cmd, "test", "push"), cmd)

    def test_a_failure_does_not_stop_the_push(self):
        for cmd in ("npm test; git push",
                    "npm test\ngit push",
                    "npm test 2>&1 | tail -5 && git push",
                    "npm test || true; git push",
                    "npm test || echo failed && git push",
                    "true || npm test && git push",
                    "! npm test && git push",
                    "set -e; if npm test; then git push; fi",
                    "set -e; for f in a b; do npm test; done; git push",
                    "npm test & git push",
                    "(set -e; npm test); git push",
                    "npm test && zsh -c 'git push'",
                    "cd x && set -e && npm test; git push"):
            self.assertFalse(gate(cmd, "test", "push"), cmd)

    def test_whole_command_status(self):
        self.assertTrue(gate("npm test", "test"))
        self.assertTrue(gate("cd app && npm test", "test"))
        self.assertTrue(gate("set -o pipefail; npm test | tail -20", "test"))
        self.assertFalse(gate("npm test 2>&1 | tail -20", "test"))
        self.assertFalse(gate("npm test; echo done", "test"))

    def test_steps_inside_conditions_and_wrappers_are_seen(self):
        self.assertIn("push", classify.command_steps("if npm test; then git push; fi"))
        self.assertEqual(classify.command_steps("screen -dmS sb zsh -c 'cd /w && npm publish'"), ["publish"])
        self.assertEqual(classify.command_steps('D="/w/New project"; cd "$D" && npm test'), ["test"])
        self.assertEqual(classify.command_steps("find . -name '*.orig' | xargs rm"), ["inspect", "edit"])


class WritesTest(unittest.TestCase):
    def w(self, command, root="/w", cwd="/w"):
        segs = classify.plan(command, None, cwd)
        found = [classify.writes(s, root, command) for s in segs]
        return "yes" if "yes" in found else ("maybe" if "maybe" in found else None)

    def test_writes(self):
        self.assertEqual(self.w("sed -i '' 's/a/b/' src/a.ts"), "yes")
        self.assertEqual(self.w("cat > notes.md <<'EOF'\nhello\nEOF"), "yes")
        self.assertEqual(self.w("echo x >> CHANGELOG.md"), "yes")
        self.assertEqual(self.w("git pull --rebase"), "yes")
        self.assertEqual(self.w("git checkout main"), "yes")
        self.assertEqual(self.w("prettier --write ."), "yes")
        self.assertEqual(self.w("rm -rf src/old"), "yes")
        self.assertEqual(self.w('D=/w/src; cp a.txt "$D/b.txt"'), "yes")
        self.assertEqual(self.w("npm install lodash"), "maybe")
        self.assertEqual(self.w("python3 - <<'EOF'\nopen('/w/a.txt', 'w').write('x')\nEOF"), "maybe")
        self.assertEqual(self.w("npm test > /w/test.log"), "maybe")

    def test_does_not_write(self):
        self.assertIsNone(self.w("echo x > /tmp/y"))
        self.assertIsNone(self.w("npm test 2>&1 | tee /tmp/log"))
        self.assertIsNone(self.w("cd /tmp && echo x > y"))
        self.assertIsNone(self.w("cp a.txt /elsewhere/b.txt"))
        self.assertIsNone(self.w("python3 - <<'EOF'\nopen('/Users/x/notes.md', 'w').write('x')\nEOF"))
        self.assertIsNone(self.w('node -e "const f = a => a > 1"'))
        self.assertIsNone(self.w("rm -rf node_modules dist"))
        self.assertIsNone(self.w("npm test 2>/dev/null"))
        self.assertIsNone(self.w("git status && git diff > /dev/null"))
        self.assertIsNone(self.w("sed -n '1,20p' src/a.ts"))

    def test_directories(self):
        segs = classify.plan('W="/w/app"; cd "$W" && git -C /w/other push && npm test', None, "/start")
        self.assertEqual([s.dir for s in segs if s.steps], ["/w/other", "/w/app"])
        segs = classify.plan("(cd sub && npm test) && git push", None, "/w")
        self.assertEqual([(s.steps, s.dir) for s in segs if s.steps], [(["test"], "/w/sub"), (["push"], "/w")])


class VerdictTest(unittest.TestCase):
    def test_runners(self):
        cases = {
            " Test Files  1 failed | 3 passed (4)\n      Tests  2 failed | 40 passed (42)": "fail",
            " Test Files  4 passed (4)\n      Tests  42 passed (42)": "pass",
            "Tests:       1 failed, 12 passed, 13 total": "fail",
            "Tests:       13 passed, 13 total": "pass",
            "===== 3 failed, 10 passed in 1.23s =====": "fail",
            "============ 10 passed in 0.52s ============": "pass",
            "# tests 12\n# pass 12\n# fail 0": "pass",
            "# tests 12\n# pass 10\n# fail 2": "fail",
            "ℹ tests 5\nℹ pass 5\nℹ fail 0": "pass",
            "ok  \tgithub.com/x/y\t0.51s": "pass",
            "--- FAIL: TestX (0.00s)\nFAIL": "fail",
            "test result: ok. 5 passed; 0 failed": "pass",
            "test result: FAILED. 4 passed; 1 failed": "fail",
            "  12 passing (2s)\n  1 failing": "fail",
            "Ran 18 tests in 0.5s\n\nOK": "pass",
            "FAILED (failures=1)": "fail",
            "npm error Lifecycle script `test` failed with error": "fail",
            "just some output": None,
        }
        for text, want in cases.items():
            self.assertEqual(test_verdict(text), want, text)

    def test_escaped_json_line(self):
        raw = '{"output":"Ran 3 tests in 0.1s\\n\\nOK\\nProcess exited with code 0"}'
        self.assertEqual(test_verdict(raw), "pass")


if __name__ == "__main__":
    unittest.main()
