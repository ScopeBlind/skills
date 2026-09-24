"""Install, approvals, Codex, the standard's page, labels, sharing and the digest. Run: python3 -m unittest discover -s scripts/tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

import replay  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
STARTER = os.path.join(os.path.dirname(SCRIPTS), "assets", "starter-rules.json")
HOOK = os.path.join(SCRIPTS, "enforce_hook.py")
HAS_GIT = shutil.which("git") is not None


def _load(path):
    with open(path) as fh:
        return json.load(fh)


class Sandbox(unittest.TestCase):
    """Every test gets its own HOME, replay state and Codex home, so nothing touches the real ones."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.env = dict(os.environ, HOME=self.tmp, SCOPEBLIND_REPLAY_HOME=os.path.join(self.tmp, "replay"),
                        CODEX_HOME=os.path.join(self.tmp, "codex"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args, stdin=""):
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "replay.py")] + list(args), input=stdin,
                           capture_output=True, text=True, timeout=120, env=self.env, cwd=getattr(self, "cwd", self.tmp))
        return p

    def hook(self, command, rules_path, agent="claude-code", session="s1", tid="t1"):
        event = {"hook_event_name": "PreToolUse", "session_id": session, "cwd": self.tmp, "tool_name": "Bash",
                 "tool_input": {"command": command}, "tool_use_id": tid, "transcript_path": None}
        args = [sys.executable, HOOK, "--rules", rules_path] + (["--agent", "codex"] if agent == "codex" else [])
        p = subprocess.run(args, input=json.dumps(event), capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else None


class CodexTrustTest(unittest.TestCase):
    def test_matches_codex_0_145(self):
        # Vectors computed by the Codex 0.145.0 binary itself.
        self.assertEqual(replay.codex_trust_hash({"matcher": "", "hooks": [{"type": "command", "command": "node /abs/path/gate.mjs", "timeout": 60}]}),
                         "sha256:618349d8e5c6a70a2271dc940821c3e9d8e3a65feb93ff77d614dbae9068f06f")
        self.assertEqual(replay.codex_trust_hash({"hooks": [{"type": "command", "command": "node /abs/path/gate.mjs"}]}),
                         "sha256:91f1f97c2f6b2633856e8016a4a3e91957842319e97386297eaad23488cfe746")


@unittest.skipUnless(HAS_GIT, "git is not installed")
class InstallTest(Sandbox):
    def test_install_is_reversible_and_keeps_other_hooks(self):
        repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        self.cwd = repo
        os.makedirs(os.path.join(repo, ".claude"))
        settings = os.path.join(repo, ".claude", "settings.json")
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo other"}]}
        with open(settings, "w") as fh:
            json.dump({"model": "x", "hooks": {"PreToolUse": [other]}}, fh)
        rules_path = os.path.join(repo, ".claude", "scopeblind-rules.json")
        shutil.copyfile(STARTER, rules_path)
        dry = self.cli("install", "--scope", "project", "--agent", "both", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Dry run", dry.stdout)
        self.assertEqual(_load(settings)["hooks"]["PreToolUse"], [other])
        self.assertFalse(os.path.exists(os.path.join(repo, ".codex", "hooks.json")))
        for _ in range(2):  # installing twice leaves one copy
            p = self.cli("install", "--scope", "project", "--agent", "both", "--trust")
            self.assertEqual(p.returncode, 0, p.stderr)
        data = _load(settings)
        self.assertEqual(data["model"], "x")
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], other)
        self.assertEqual(sum("enforce_hook.py" in h["command"] for e in pre for h in e["hooks"]), 1)
        self.assertTrue(any(n.startswith("settings.json.scopeblind-backup-") for n in os.listdir(os.path.dirname(settings))))
        codex_hooks = _load(os.path.join(repo, ".codex", "hooks.json"))
        self.assertEqual(set(codex_hooks), {"hooks"})
        entry = codex_hooks["hooks"]["PreToolUse"][0]
        self.assertIn("--agent codex", entry["hooks"][0]["command"])
        with open(os.path.join(self.tmp, "codex", "config.toml")) as fh:
            config = fh.read()
        key = "%s:pre_tool_use:0:0" % os.path.join(repo, ".codex", "hooks.json")
        self.assertIn('[hooks.state."%s"]' % key, config)
        self.assertIn(replay.codex_trust_hash(entry), config)
        self.assertEqual(config.count("[hooks.state."), 1)
        status = self.cli("status")
        self.assertIn("Claude Code, this project: hook installed", status.stdout)
        p = self.cli("uninstall", "--scope", "project", "--agent", "both")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(_load(settings)["hooks"]["PreToolUse"], [other])
        self.assertNotIn("PreToolUse", _load(os.path.join(repo, ".codex", "hooks.json"))["hooks"])

    @unittest.skipUnless(os.name == "posix", "the check is a POSIX shell test")
    def test_a_missing_hook_script_does_not_refuse_every_call(self):
        repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        self.cwd = repo
        os.makedirs(os.path.join(repo, ".claude"))
        shutil.copyfile(STARTER, os.path.join(repo, ".claude", "scopeblind-rules.json"))
        p = self.cli("install", "--scope", "project", "--agent", "claude-code")
        self.assertEqual(p.returncode, 0, p.stderr)
        settings = os.path.join(repo, ".claude", "settings.json")
        data = _load(settings)
        handler = data["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertTrue(handler["command"].startswith('test -f "%s" && ' % replay.HOOK))
        # The skill moves: Python would exit 2 ("can't open file"), which Claude Code reads as "refuse this call".
        handler["command"] = handler["command"].replace(replay.HOOK, os.path.join(self.tmp, "moved", "enforce_hook.py"))
        with open(settings, "w") as fh:
            json.dump(data, fh)
        run = subprocess.run(["sh", "-c", handler["command"]], input="{}", capture_output=True, text=True, timeout=30)
        self.assertNotEqual(run.returncode, 2)
        self.assertIn("its script is missing", self.cli("status").stdout)


class CodexHoldTest(Sandbox):
    def test_codex_cannot_ask_so_the_person_approves_from_their_terminal(self):
        rules_path = os.path.join(self.tmp, "rules.json")
        shutil.copyfile(STARTER, rules_path)
        out = self.hook("npx wrangler pages deploy dist", rules_path, agent="codex")
        self.assertEqual(set(out), {"hookSpecificOutput"})
        self.assertEqual(set(out["hookSpecificOutput"]), {"hookEventName", "permissionDecision", "permissionDecisionReason"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("approve approve-releases", reason)
        self.assertIn("--session s1", reason)
        # The agent cannot approve for the person.
        guard = self.hook("python3 replay.py approve approve-releases --minutes 30", rules_path, agent="codex")
        self.assertEqual(guard["hookSpecificOutput"]["permissionDecision"], "deny")
        p = self.cli("approve", "approve-releases", "--minutes", "30", "--session", "s1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIsNone(self.hook("npx wrangler pages deploy dist", rules_path, agent="codex"))
        self.assertIsNotNone(self.hook("npx wrangler pages deploy dist", rules_path, agent="codex", session="s2"))
        listed = self.cli("approve", "--list")
        self.assertIn("approve-releases", listed.stdout)
        # In Claude Code the same request becomes a question for the person.
        asked = self.hook("python3 replay.py approve approve-releases --minutes 30", rules_path)
        self.assertEqual(asked["hookSpecificOutput"]["permissionDecision"], "ask")


class _Page(BaseHTTPRequestHandler):
    store = {}
    posts = []

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        hid = (q.get("held") or [None])[0]
        if hid:
            return self._send(200, {"ok": True, "held": self.store[hid]}) if hid in self.store else \
                self._send(404, {"ok": False, "error": "held_not_found"})
        return self._send(200, {"ok": True, "standard": {"request_id": "req-1"}})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(403, {"ok": False, "error": "bad_token"})
        self.posts.append(body)
        held = dict(body, decision=None)
        self.store.setdefault(body["hid"], held)
        return self._send(200, {"ok": True, "held": held, "url": "https://page.example/standard?s=x#held-" + body["hid"]})

    def log_message(self, *args):
        pass


class PageHoldTest(Sandbox):
    def setUp(self):
        super().setUp()
        _Page.store.clear()
        del _Page.posts[:]
        self.server = HTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        report = "http://127.0.0.1:%d/api/standard?s=%s" % (self.server.server_port, "a" * 24)
        state = os.path.join(self.tmp, "replay", "state")
        os.makedirs(state)
        with open(os.path.join(state, "page.json"), "w") as fh:
            json.dump({"report": report, "token": "tok"}, fh)
        self.rules_path = os.path.join(self.tmp, "rules.json")
        with open(self.rules_path, "w") as fh:
            json.dump({"version": 1, "name": "page", "rules": [
                {"id": "approve-deploys", "type": "hold", "says": "A named person approves each deploy.",
                 "match": {"steps": ["deploy"]}, "approver": "page", "approval_minutes": 30}]}, fh)

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        super().tearDown()

    def test_a_person_decides_on_the_page(self):
        out = self.hook("npx wrangler pages deploy dist", self.rules_path, agent="codex")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("waiting for the person named on the standard", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(_Page.posts), 1)
        posted = _Page.posts[0]
        self.assertTrue(posted["readback"]["summary"].startswith("Deploy: npx wrangler pages deploy dist"))
        hid = posted["hid"]
        self.assertEqual(len(hid), 24)
        still = self.hook("npx wrangler pages deploy dist", self.rules_path)
        self.assertIn("Still waiting", still["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(_Page.posts), 1)  # the same exact action is not posted twice
        _Page.store[hid]["decision"] = {"hid": hid, "payload_hash": posted["readback"]["payload_hash"],
                                        "decision": "approve", "approver": {"name": "Pat"}}
        self.assertIsNone(self.hook("npx wrangler pages deploy dist", self.rules_path))
        # One approval covers the rule's other deploys in the session for approval_minutes.
        self.assertIsNone(self.hook("npx wrangler pages deploy other", self.rules_path, tid="t2"))
        with open(os.path.join(self.tmp, "replay", "decisions.jsonl")) as fh:
            last = [json.loads(x) for x in fh][-2:]
        self.assertEqual([d["decision"] for d in last], ["covered", "covered"])
        self.assertEqual(last[0]["approver"], "Pat")

    def test_declined_and_unreachable(self):
        self.hook("npx wrangler pages deploy dist", self.rules_path, session="s9")
        hid = _Page.posts[-1]["hid"]
        _Page.store[hid]["decision"] = {"hid": hid, "payload_hash": _Page.posts[-1]["readback"]["payload_hash"],
                                        "decision": "deny", "approver": {"name": "Pat"}}
        out = self.hook("npx wrangler pages deploy dist", self.rules_path, session="s9")
        self.assertIn("Pat declined", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.server.shutdown()
        self.server.server_close()
        self.server = None  # the page is now unreachable
        out = self.hook("npx wrangler pages deploy elsewhere", self.rules_path, session="s10")
        self.assertIn("could not be reached", out["hookSpecificOutput"]["permissionDecisionReason"])


class LabelShareDigestTest(Sandbox):
    def fixtures(self):
        return ["--claude-dir", os.path.join(FIX, "claude"), "--codex-dir", os.path.join(FIX, "codex")]

    def test_label(self):
        rules_path = os.path.join(self.tmp, "rules.json")
        shutil.copyfile(STARTER, rules_path)
        dry = self.cli("label", "--rules", rules_path, "--as", "deploy", "--pattern", r"sync\.js", "--days", "100000",
                       "--dry-run", *self.fixtures())
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("matches 1 commands", dry.stdout)
        self.assertNotIn("steps", {k for k, v in _load(rules_path).items() if v})
        p = self.cli("label", "--rules", rules_path, "--as", "deploy", "--pattern", r"sync\.js", "--days", "100000", *self.fixtures())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(_load(rules_path)["steps"], {"deploy": [r"sync\.js"]})
        bad = self.cli("label", "--rules", rules_path, "--as", "Deploy!", "--pattern", "x", *self.fixtures())
        self.assertEqual(bad.returncode, 2)

    def test_share(self):
        out = os.path.join(self.tmp, "share")
        p = self.cli("share", "--rules", STARTER, "--out", out, "--days", "100000", *self.fixtures())
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(out, "README.md")) as fh:
            readme = fh.read()
        self.assertIn("A person reviews each case of deploying, publishing and merging a pull request before it runs.", readme)
        self.assertIn("not uploaded", readme)
        with open(os.path.join(out, "calls.jsonl")) as fh:
            text = fh.read()
        calls = [json.loads(x) for x in text.splitlines() if x.strip()]
        self.assertEqual(len(calls), 17)
        self.assertNotIn("abcd1234efgh5678", text)

    def test_digest_and_session_start(self):
        p = self.cli("digest", "--since", "2020-01-01", "--quiet", *self.fixtures())
        self.assertEqual(p.returncode, 0, p.stderr)
        latest = _load(os.path.join(self.tmp, "replay", "latest-weekly.json"))
        self.assertTrue(os.path.isfile(latest["digest"]))
        run = lambda: subprocess.run([sys.executable, os.path.join(SCRIPTS, "digest_hook.py")], input="{}",  # noqa: E731
                                     capture_output=True, text=True, timeout=30, env=self.env).stdout
        self.assertEqual(run(), "")  # off until the person turns it on
        self.assertEqual(self.cli("weekly", "on").returncode, 0)
        first = run()
        self.assertIn("Weekly replay", first)
        self.assertEqual(run(), "")  # shown once


if __name__ == "__main__":
    unittest.main()
