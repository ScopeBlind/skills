"""Live decisions: requirements, fingerprints and the hook end to end. Run: python3 -m unittest discover -s scripts/tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

from sbreplay import classify, fingerprint, rules  # noqa: E402
from sbreplay.sources import Action  # noqa: E402

STARTER = os.path.join(os.path.dirname(SCRIPTS), "assets", "starter-rules.json")
HOOK = os.path.join(SCRIPTS, "enforce_hook.py")
HAS_GIT = shutil.which("git") is not None


def act(aid, command=None, tool="Bash", outcome="ok", at=0, verdict=None, target=None, project="/w"):
    return Action(id=aid, agent="claude-code", session="s", project=project, ts=1000.0 + at, tool=tool,
                  command=command, target=target, steps=classify.tool_steps(tool, command), outcome=outcome,
                  verdict=verdict)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false"] + list(args), check=True, capture_output=True)


def make_repo(root):
    repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(repo, "dist"))
    git(root, "init", "-q", repo)
    for name, text in (("a.txt", "one\n"), ("dist/app.js", "x\n")):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start")
    return repo


def write(repo, name, text):
    with open(os.path.join(repo, name), "w") as fh:
        fh.write(text)


class RequirementTest(unittest.TestCase):
    def setUp(self):
        self.doc = rules.load(STARTER)

    def decide(self, action, prior=(), **kw):
        return rules.decide(self.doc, action, list(prior), **kw)

    def test_history(self):
        test_ok = act("a", "npm test", at=0)
        push = act("p", "git push origin x", at=60)
        self.assertIsNone(self.decide(push, [test_ok])[0])
        d = self.decide(push, [])
        self.assertEqual(d[0], "deny")
        self.assertIn("Tests did not run", d[2])
        failed = act("a", "npm test", outcome="failed")
        self.assertIn("failed the last time", self.decide(push, [failed])[2])

    def test_same_command(self):
        self.assertIsNone(self.decide(act("p", "npm test && git push origin x"))[0])
        for cmd, join in (("npm test 2>&1 | tail -5 && git push origin x", "|"), ("npm test; git push origin x", ";")):
            d = self.decide(act("p", cmd))
            self.assertEqual(d[0], "deny", cmd)
            self.assertIn("would not stop it from pushing", d[2])
        self.assertIn("changes files after", self.decide(act("p", "npm test && sed -i '' s/a/b/ a.txt && git push"))[2])

    def test_hidden_results(self):
        piped = act("a", "npm test 2>&1 | tail -5")
        push = act("p", "git push origin x", at=60)
        d = self.decide(push, [piped])
        self.assertEqual(d[0], "deny")
        self.assertIn("result is unknown", d[2])
        self.assertIn("pipefail", d[2])
        self.assertIsNone(self.decide(push, [act("a", "npm test 2>&1 | tail -5", verdict="pass")])[0])
        self.assertIn("failed", self.decide(push, [act("a", "npm test 2>&1 | tail -5", verdict="fail")])[2])

    def test_changes_after_the_test(self):
        test_ok = act("a", "npm test")
        push = act("p", "git push origin x", at=120)
        shell_edit = act("e", "sed -i '' 's/a/b/' src/a.ts", at=60)
        d = self.decide(push, [test_ok, shell_edit])
        self.assertEqual(d[0], "deny")
        self.assertIn("sed -i", d[2])
        self.assertIsNone(self.decide(push, [test_ok, act("e", "echo x > /tmp/y", at=60)])[0])
        tool_edit = act("e", tool="Edit", target="/w/src/a.ts", at=60)
        self.assertEqual(self.decide(push, [test_ok, tool_edit])[0], "deny")
        # The content fingerprint is authoritative: an edit that was undone is not a change; a change the log never
        # saw is.
        same = lambda *a: ("same", [])  # noqa: E731
        changed = lambda *a: ("changed", ["src/a.ts", "README.md"])  # noqa: E731
        self.assertIsNone(self.decide(push, [test_ok, tool_edit], fingerprint=same)[0])
        d = self.decide(push, [test_ok], fingerprint=changed)
        self.assertEqual(d[0], "deny")
        self.assertIn("src/a.ts, README.md", d[2])

    def test_one_approval_covers_the_task(self):
        first = act("d1", "npx wrangler pages deploy dist", at=0)
        second = act("d2", "npx wrangler pages deploy dist", at=600)
        d = self.decide(first)
        self.assertEqual(d[0], "ask")
        self.assertIn("next 60 minutes", d[2])
        asked = [{"rule": "approve-releases", "tool_use_id": "d1", "ts": first.ts}]
        covered = self.decide(second, [first], asked=asked)
        self.assertIsNone(covered[0])
        self.assertEqual(covered[3].get("covered_by"), "d1")
        refused = act("d1", "npx wrangler pages deploy dist", outcome="denied")
        self.assertEqual(self.decide(second, [refused], asked=asked)[0], "ask")
        late = act("d3", "npx wrangler pages deploy dist", at=3 * 3600)
        self.assertEqual(self.decide(late, [first], asked=asked)[0], "ask")

    def test_a_terminal_approval_lets_a_requirement_go_ahead(self):
        push = act("p", "git push origin docs-only", at=60)
        self.assertEqual(self.decide(push)[0], "deny")
        grant = {"id": "g1", "rule": "tests-before-push", "session": "s", "at": 1000.0, "until": 2000.0}
        d = self.decide(push, grants=[grant])
        self.assertIsNone(d[0])
        self.assertEqual(d[3].get("covered_by"), "grant:g1")
        self.assertEqual(self.decide(push, grants=[dict(grant, session="other")])[0], "deny")
        self.assertEqual(self.decide(push, grants=[dict(grant, until=1030.0)])[0], "deny")

    def test_block_and_limit_messages(self):
        d = self.decide(act("f", "git push --force origin main"))
        self.assertEqual(d[0], "deny")
        self.assertIn("do not retry", d[2])


@unittest.skipUnless(HAS_GIT, "git is not installed")
class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_compare(self):
        root = fingerprint.repo_root(self.repo)
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.repo))
        s0 = fingerprint.snapshot(root)
        self.assertEqual(fingerprint.compare(s0, fingerprint.snapshot(root)), ("same", []))
        write(self.repo, "a.txt", "two\n")
        tested = fingerprint.snapshot(root)
        git(self.repo, "commit", "-qam", "two")
        committed = fingerprint.snapshot(root)
        self.assertEqual(fingerprint.compare(tested, committed, "head"), ("same", []))
        self.assertEqual(fingerprint.compare(tested, committed, "worktree"), ("same", []))
        write(self.repo, "a.txt", "three\n")
        edited = fingerprint.snapshot(root)
        self.assertEqual(fingerprint.compare(tested, edited, "worktree"), ("changed", ["a.txt"]))
        self.assertEqual(fingerprint.compare(tested, edited, "head"), ("same", []))  # the push still sends "two"
        git(self.repo, "commit", "-qam", "three")
        self.assertEqual(fingerprint.compare(tested, fingerprint.snapshot(root), "head"), ("changed", ["a.txt"]))

    def test_untracked_and_ignored(self):
        root = fingerprint.repo_root(self.repo)
        write(self.repo, "notes.txt", "scratch\n")
        tested = fingerprint.snapshot(root)
        os.remove(os.path.join(self.repo, "notes.txt"))
        now = fingerprint.snapshot(root)
        self.assertEqual(fingerprint.compare(tested, now, "head"), ("same", []))
        self.assertEqual(fingerprint.compare(tested, now, "worktree"), ("changed", ["notes.txt"]))
        write(self.repo, "dist/app.js", "rebuilt\n")
        self.assertEqual(fingerprint.compare(now, fingerprint.snapshot(root), "worktree", ["dist/**"]), ("same", []))


@unittest.skipUnless(HAS_GIT, "git is not installed")
class HookEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = make_repo(self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.log = os.path.join(self.home, "decisions.jsonl")
        self.rules = os.path.join(self.tmp, "rules.json")
        shutil.copyfile(STARTER, self.rules)
        self.transcript = os.path.join(self.tmp, "t.jsonl")
        open(self.transcript, "w").close()
        self.clock = datetime.now(timezone.utc) - timedelta(minutes=30)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def record(self, tid, command, output="ok", error=False, tool="Bash", target=None):
        """Append a finished tool call to the transcript, as Claude Code does."""
        self.clock += timedelta(seconds=30)
        ts = self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")
        inp = {"command": command} if command else {"file_path": target}
        lines = [{"type": "assistant", "timestamp": ts, "sessionId": "s1", "cwd": self.repo,
                  "message": {"content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}},
                 {"type": "user", "timestamp": ts, "sessionId": "s1",
                  "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": output, "is_error": error}]}}]
        with open(self.transcript, "a") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))

    def hook(self, tid, command=None, tool="Bash", target=None, session="s1", transcript=None):
        event = {"hook_event_name": "PreToolUse", "session_id": session, "cwd": self.repo, "tool_name": tool,
                 "transcript_path": transcript or self.transcript, "tool_use_id": tid,
                 "tool_input": {"command": command} if command else {"file_path": target}}
        p = subprocess.run([sys.executable, HOOK, "--rules", self.rules, "--log", self.log], input=json.dumps(event),
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, SCOPEBLIND_REPLAY_HOME=self.home))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        out = json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None
        return (out["permissionDecision"], out["permissionDecisionReason"]) if out else (None, None)

    def run_test(self, tid, command="npm test", output=" Test Files  3 passed (3)\n      Tests  12 passed (12)"):
        self.assertEqual(self.hook(tid, command), (None, None))
        self.record(tid, command, output)

    def test_push_is_checked_against_the_files_the_tests_saw(self):
        self.run_test("t1")
        self.assertEqual(self.hook("t2", "git push origin feature")[0], None)
        # A change the agent's log never saw: someone commits an edit after the tests ran.
        write(self.repo, "a.txt", "changed outside the agent\n")
        git(self.repo, "commit", "-qam", "outside")
        decision, reason = self.hook("t3", "git push origin feature")
        self.assertEqual(decision, "deny")
        self.assertIn("a.txt", reason)
        self.assertIn("npm test", reason)
        # Piped test output hides failures unless the runner's summary says otherwise.
        self.run_test("t4", "npm test 2>&1 | tail -5", output="some output with no summary")
        self.assertIn("result is unknown", self.hook("t5", "git push origin feature")[1])
        self.run_test("t6", "npm test 2>&1 | tail -5")
        self.assertEqual(self.hook("t7", "git push origin feature")[0], None)

    def test_a_refusal_says_how_the_person_can_let_it_go_ahead(self):
        decision, reason = self.hook("n1", "git push origin docs-only")
        self.assertEqual(decision, "deny")
        self.assertIn("did not run", reason)
        self.assertIn("approve tests-before-push --minutes 30 --session s1", reason)

    def test_same_command_and_approvals(self):
        empty = os.path.join(self.tmp, "empty.jsonl")
        open(empty, "w").close()
        decision, reason = self.hook("x1", "npm test; git push origin feature", session="s2", transcript=empty)
        self.assertEqual(decision, "deny")
        self.assertIn("&&", reason)
        self.assertEqual(self.hook("d1", "npx wrangler pages deploy dist")[0], "ask")
        self.record("d1", "npx wrangler pages deploy dist")
        self.assertEqual(self.hook("d2", "npx wrangler pages deploy dist")[0], None)
        with open(self.log) as fh:
            decisions = [json.loads(x) for x in fh]
        self.assertEqual([d["decision"] for d in decisions][-2:], ["ask", "covered"])
        self.assertEqual(decisions[-1]["covered_by"], "d1")

    def test_guards(self):
        state = os.path.join(self.home, "state", "asks-s1.json")
        self.assertEqual(self.hook("g1", "echo '[]' > %s" % state)[0], "deny")
        self.assertEqual(self.hook("g2", tool="Edit", target=self.rules)[0], "ask")
        self.assertEqual(self.hook("g3", tool="Write", target=os.path.join(self.repo, ".claude", "settings.json"))[0], "ask")
        self.assertEqual(self.hook("g4", "ls -la"), (None, None))
