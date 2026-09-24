"""Tests for replay-and-discover. Run: python3 -m unittest discover -s scripts/tests -v"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

from sbreplay import classify, redact, rules  # noqa: E402
from sbreplay.discover import discover  # noqa: E402
from sbreplay.sources import Action, load_actions, read_claude, read_codex  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
CLAUDE = os.path.join(FIX, "claude")
CODEX = os.path.join(FIX, "codex")
STARTER = os.path.join(os.path.dirname(SCRIPTS), "assets", "starter-rules.json")


def fixture_actions():
    return load_actions(claude_dir=CLAUDE, codex_dir=CODEX)


class ClassifyTest(unittest.TestCase):
    def check(self, command, expected):
        self.assertEqual(classify.command_steps(command), expected, command)

    def test_commands(self):
        self.check('git commit -m "fix; npm publish" && git push origin main', ["commit", "push"])
        self.check("screen -dmS sb zsh -c '~/bin/deploy-run.sh > log 2>&1'", ["deploy"])
        self.check("cd app && npx wrangler pages deploy dist", ["deploy"])
        self.check('git -C "/path with space" push --force-with-lease', ["force_push"])
        self.check("python3 - <<'EOF'\nnpm publish\nEOF", ["other_command"])
        self.check("npm run -s test:unit", ["test"])
        self.check("tsc --noEmit -p .", ["typecheck"])
        self.check("npm run build:prod", ["build"])
        self.check("npm publish --dry-run", ["other_command"])
        self.check("npm publish", ["publish"])
        self.check("sudo rm -rf /opt/x", ["permissions", "delete"])
        self.check('FOO=bar BAZ="a b" pytest -q', ["test"])
        self.check("curl -s https://example.com", ["web_request"])
        self.check('echo "git push"', ["other_command"])
        self.check("gh pr merge 12 --squash", ["pr_merge"])
        self.check("wrangler pages deployment list --project-name x", ["other_command"])
        self.check("git push -n origin main", ["other_command"])
        self.check("node --import tsx --test a.test.ts 2>&1 | tail -5", ["test", "other_command"])

    def test_tools(self):
        self.assertEqual(classify.tool_steps("Edit"), ["edit"])
        self.assertEqual(classify.tool_steps("mcp__github__create_pr"), ["mcp"])
        self.assertEqual(classify.tool_steps("Read"), ["read"])
        self.assertEqual(classify.tool_steps("Bash", "ls"), ["other_command"])

    def test_extra_steps(self):
        extra = classify.compile_extra({"live_check": ["curl .*example\\.com"]})
        self.assertEqual(classify.command_steps("curl -s https://example.com/", extra), ["live_check"])

    def test_words(self):
        self.assertEqual(classify.gerund("pr_open"), "opening a pull request")
        self.assertEqual(classify.noun("ci_check"), "a CI check")
        self.assertEqual(classify.lc("Check CI"), "check CI")


class RedactTest(unittest.TestCase):
    def test_redact(self):
        out = redact.redact("curl -H 'Authorization: Bearer abcdefghijklmnop' https://u:pw12345@x.io ghp_" + "a" * 36)
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertNotIn("pw12345", out)
        self.assertNotIn("a" * 36, out)
        self.assertEqual(redact.redact(os.path.expanduser("~") + "/x"), "~/x")
        self.assertTrue(redact.redact("x" * 300).endswith("…"))

    def test_find_secrets(self):
        self.assertTrue(redact.find_secrets("WITNESS_SECRET=abc123def456ghi node a.js"))
        self.assertTrue(redact.find_secrets("npm config set //registry/:_authToken=npm_" + "b" * 36))
        self.assertEqual(redact.find_secrets("TOKEN=$X node a"), [])
        self.assertEqual(redact.find_secrets("API_TOKEN=changeme x"), [])
        self.assertEqual(redact.find_secrets("python3 - <<EOF\nsecret = 'abcdefghijk'\nEOF"), [])
        self.assertEqual(redact.find_secrets("MAX_TOKENS=100000000 run"), [])


class SourcesTest(unittest.TestCase):
    def test_claude(self):
        acts = {a.id: a for a in read_claude(CLAUDE)}
        self.assertEqual(len(acts), 11)  # t1 is not duplicated from the resumed session; journal.jsonl is skipped
        self.assertEqual(acts["t2"].outcome, "failed")
        self.assertEqual(acts["t2"].exit_code, 1)
        self.assertEqual(acts["t4"].outcome, "ok")
        self.assertEqual(acts["t8"].outcome, "denied")
        self.assertEqual(acts["t5"].steps, ["commit", "push"])
        self.assertEqual(acts["t10"].steps, ["other_command"])
        self.assertAlmostEqual(acts["t2"].duration, 20.0)
        self.assertEqual(acts["t11"].session, "s2")

    def test_codex(self):
        acts = {a.id: a for a in read_codex(CODEX)}
        self.assertEqual(acts["call_1"].outcome, "ok")
        self.assertEqual(acts["call_1"].duration, 3.5)
        self.assertEqual(acts["call_2"].steps, ["push"])
        self.assertNotIn("dup", acts)  # newer files use CommandExecution events, not the call log
        self.assertEqual(acts["x1"].steps, ["build"])
        self.assertEqual(acts["x1"].duration, 12)
        self.assertEqual(acts["x4"].outcome, "failed")
        self.assertEqual(acts["x4"].steps, ["deploy"])
        self.assertEqual(acts["x2"].steps, ["edit"])
        self.assertEqual(acts["x3"].outcome, "failed")
        self.assertEqual(acts["x3"].duration, 1.5)
        self.assertEqual(acts["x2"].project, "/work/web app")

    def test_window_and_project(self):
        since = 1790000000  # 2026-09-21T13:46:40Z: excludes everything but nothing is after it
        self.assertEqual(load_actions(claude_dir=CLAUDE, codex_dir=CODEX, since=since), [])
        only = load_actions(claude_dir=CLAUDE, codex_dir=CODEX, project="svc")
        self.assertEqual({a.id for a in only}, {"call_1", "call_2"})


class DiscoverTest(unittest.TestCase):
    def test_findings(self):
        f = discover(fixture_actions())
        push = f["definition_of_done"]["push"]
        self.assertEqual(push["n"], 2)
        self.assertEqual(push["tested_final_version"], 2)  # Claude: test passed after the last edit; Codex: pytest passed
        self.assertEqual(f["after_checks"]["deploy"]["checked_after"], 1)
        self.assertEqual(f["secrets"]["commands"], 1)
        self.assertEqual(f["risky"]["force_push"]["refused"], 1)
        self.assertEqual(f["friction"]["refused"], 1)
        self.assertEqual(f["rework"]["fix_and_rerun_loops"], 1)
        self.assertEqual(f["overview"]["by_agent"]["claude-code"]["sessions"], 2)
        titles = [s["title"] for s in f["suggestions"]]
        self.assertIn("Keep secrets out of commands", titles)
        text = json.dumps(f)
        self.assertNotIn("abcd1234efgh5678", text)  # the literal secret never reaches findings


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.doc = rules.load(STARTER)

    def test_starter_is_sound(self):
        self.assertEqual(rules.problems(self.doc), [])
        rows = rules.readback(self.doc)
        self.assertTrue(all(r["how"] for r in rows))
        self.assertFalse([r for r in rows if r["id"] == "check-after-deploy"][0]["enforced_live"])

    def test_rehearse(self):
        r = {x["id"]: x for x in rules.rehearse(self.doc, fixture_actions(), days=30)["rules"]}
        self.assertEqual(r["approve-releases"]["would_hold"], 2)          # Claude deploy + Codex deploy
        self.assertEqual(r["tests-before-push"]["satisfied"], 2)          # both pushes had passing tests on the final version
        self.assertEqual(r["tests-before-push"]["violations"], 1)         # the pull request in s2 had no tests
        self.assertEqual(r["no-force-push-main"]["would_block"], 1)
        self.assertEqual(r["no-force-push-main"]["already_refused"], 1)
        self.assertEqual(r["no-literal-secrets"]["would_block"], 1)
        self.assertEqual(r["check-after-deploy"]["satisfied"], 1)
        self.assertEqual(r["check-after-deploy"]["violations"], 1)
        self.assertNotIn("abcd1234efgh5678", json.dumps(r))

    def test_command_pattern_applies_to_the_matching_part(self):
        feature = Action(id="f", agent="claude-code", session="s", ts=1.0, tool="Bash", steps=["other_command", "force_push"],
                         command="git fetch origin main && git push --force-with-lease origin feat/x")
        main = Action(id="m", agent="claude-code", session="s", ts=1.0, tool="Bash", steps=["force_push"],
                      command="git push -f origin main")
        self.assertIsNone(rules.decide(self.doc, feature, [])[0])
        self.assertEqual(rules.decide(self.doc, main, [])[0], "deny")

    def test_reference_example_is_valid(self):
        import re
        with open(os.path.join(os.path.dirname(SCRIPTS), "references", "RULES.md")) as fh:
            doc = json.loads(re.search(r"```json\n(.*?)\n```", fh.read(), re.S).group(1))
        self.assertEqual(rules.problems(rules.normalize(doc)), [])

    def test_problems(self):
        bad = rules.normalize({"rules": [{"id": "x", "type": "nope"}, {"id": "x", "type": "hold", "says": "s", "match": {"steps": ["warp"]}}]})
        msgs = " ".join(rules.problems(bad))
        self.assertIn("type must be", msgs)
        self.assertIn("duplicate id", msgs)
        self.assertIn("unknown step 'warp'", msgs)

    def test_decide(self):
        base = dict(agent="claude-code", session="s", project="/w", ts=1000.0)
        test_ok = Action(id="a", tool="Bash", command="npm test", steps=["test"], outcome="ok", **dict(base, ts=900.0))
        edit = Action(id="b", tool="Edit", steps=["edit"], outcome="ok", **dict(base, ts=950.0))
        push = Action(id="c", tool="Bash", command="git push origin x", steps=["push"], **base)
        self.assertEqual(rules.decide(self.doc, push, [test_ok])[0], None)
        self.assertEqual(rules.decide(self.doc, push, [test_ok, edit])[0], "ask")
        force = Action(id="d", tool="Bash", command="git push --force origin main", steps=["force_push"], **base)
        self.assertEqual(rules.decide(self.doc, force, [])[0], "deny")
        deploy = Action(id="e", tool="Bash", command="vercel --prod", steps=["deploy"], **base)
        self.assertEqual(rules.decide(self.doc, deploy, [])[0], "ask")
        limited = rules.normalize({"rules": [{"id": "l", "type": "limit", "says": "Two deploys a day.", "max": 2,
                                              "match": {"steps": ["deploy"]}}]})
        self.assertEqual(rules.decide(limited, deploy, [], {"l": 2})[0], "deny")
        self.assertEqual(rules.decide(limited, deploy, [], {"l": 1})[0], None)


class HookTest(unittest.TestCase):
    def test_hook(self):
        tmp = tempfile.mkdtemp()
        try:
            transcript = os.path.join(tmp, "t.jsonl")
            # Re-date the fixture to the last few minutes, so it falls inside the rule's look-back window.
            from datetime import datetime, timedelta, timezone
            start = datetime.now(timezone.utc) - timedelta(minutes=20)
            with open(os.path.join(CLAUDE, "-work-app", "s1.jsonl")) as src, open(transcript, "w") as dst:
                for i, line in enumerate(src):
                    entry = json.loads(line)
                    if "timestamp" in entry:
                        entry["timestamp"] = (start + timedelta(seconds=60 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    dst.write(json.dumps(entry) + "\n")
            log = os.path.join(tmp, "decisions.jsonl")

            def run(command):
                event = {"hook_event_name": "PreToolUse", "session_id": "s1", "transcript_path": transcript, "cwd": "/work/app",
                         "tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "new"}
                p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "enforce_hook.py"), "--rules", STARTER, "--log", log],
                                   input=json.dumps(event), capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr)
                return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else None

            self.assertEqual(run("git push --force origin main"), "deny")
            self.assertEqual(run("npx wrangler pages deploy dist"), "ask")
            self.assertEqual(run("git push origin feature"), None)  # tests passed after the last edit in the transcript
            self.assertEqual(run("ls -la"), None)
            with open(log) as fh:
                lines = [json.loads(x) for x in fh]
            self.assertEqual([l["decision"] for l in lines], ["deny", "ask"])
            self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
            broken = subprocess.run([sys.executable, os.path.join(SCRIPTS, "enforce_hook.py"), "--rules", STARTER, "--log", log],
                                    input="not json", capture_output=True, text=True, timeout=30)
            self.assertEqual((broken.returncode, broken.stdout), (0, ""))  # never breaks the session
        finally:
            shutil.rmtree(tmp)


class CliTest(unittest.TestCase):
    def test_scan_and_friends(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "out")
            p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "replay.py"), "scan", "--since", "2020-01-01",
                                "--claude-dir", CLAUDE, "--codex-dir", CODEX, "--rules", STARTER, "--out", out],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("## 1. What you can already show", p.stdout)
            self.assertIn("## 7. Rehearsal", p.stdout)
            self.assertNotIn("abcd1234efgh5678", p.stdout)
            for name in ("findings.json", "report.md", "report.html"):
                path = os.path.join(out, name)
                self.assertTrue(os.path.isfile(path), name)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(os.path.join(out, "report.html")) as fh:
                page = fh.read()
            self.assertNotIn("abcd1234efgh5678", page)
            self.assertIn("<title>Agent work replay</title>", page)
            p2 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "replay.py"), "scan", "--since", "2020-01-01",
                                 "--claude-dir", CLAUDE, "--codex-dir", CODEX, "--compare", os.path.join(out, "findings.json"),
                                 "--out", os.path.join(tmp, "out2"), "--quiet"], capture_output=True, text=True, timeout=120)
            self.assertEqual(p2.returncode, 0, p2.stderr)
            rules_path = os.path.join(tmp, "rules.json")
            p3 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "replay.py"), "init-rules", "--from",
                                 os.path.join(out, "findings.json"), "--out", rules_path], capture_output=True, text=True)
            self.assertEqual(p3.returncode, 0, p3.stderr)
            p4 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "replay.py"), "explain", "--rules", rules_path],
                                capture_output=True, text=True)
            self.assertEqual(p4.returncode, 0, p4.stdout + p4.stderr)
            self.assertIn("| 1 |", p4.stdout)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
