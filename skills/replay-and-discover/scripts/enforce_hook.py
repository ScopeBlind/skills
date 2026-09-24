#!/usr/bin/env python3
"""PreToolUse hook: apply a replay-and-discover rules file before each tool call, in Claude Code or Codex.

Installed by `replay.py install` (or by the plugin). Decisions:
- block: refused, and the agent is told not to retry.
- hold: waits for a person. In Claude Code, Claude Code asks them. Codex cannot ask, so the call is refused with the
  exact command the person can run in their own terminal to approve (`replay.py approve`). With "approver": "page",
  the action waits on the standard's page on scopeblind.com, where the person named on the standard decides and
  signs; the agent retries the same command once they have. With `approval_minutes`, one approval covers the rule's
  other matching calls in the same session for that long.
- require_before: refused with an exact instruction to the agent when a required step did not run, failed, had its
  result hidden, or ran before the files changed; or held / refused outright, as the rule says.
- limit: refused once reached.

Before a required check runs (a test, say), the hook fingerprints the git working copy. Before a push or deploy, it
compares the working copy with that fingerprint, so any change after the test is caught, whoever made it. It also
protects itself: its state is off limits to the agent, and changes to the rules or to agent settings wait for the
person.

If anything goes wrong (unreadable rules, unexpected input), the hook makes no decision, so the agent's normal
permission rules apply. Every decision is appended to ~/.scopeblind/replay/decisions.jsonl (readable only by you).
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    from urllib.error import HTTPError, URLError
    from urllib.parse import parse_qs, urlparse
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    HTTPError = URLError = Exception

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sbreplay import fingerprint, rules  # noqa: E402
from sbreplay.classify import SHELL_TOOLS, label, tool_plan  # noqa: E402
from sbreplay.redact import redact  # noqa: E402
from sbreplay.sources import Action, read_claude_file, read_codex_file  # noqa: E402

HOME = os.environ.get("SCOPEBLIND_REPLAY_HOME") or os.path.expanduser("~/.scopeblind/replay")
STATE = os.path.join(HOME, "state")
REPLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay.py")
TAIL_BYTES = 8 * 1024 * 1024
MAX_CHECKS = 40
_SETTINGS = re.compile(r"(?:^|/)\.(?:claude|codex)/(?:settings(?:\.local)?\.json|hooks\.json|config\.toml)$")


# ---------------------------------------------------------------------------------------------------------------
# Private files


def _private_write(path, text, append=False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if append:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(text)
        return
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _state(name, session=None):
    if session is None:
        return os.path.join(STATE, "%s.json" % name)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session or "none"))[:80]
    return os.path.join(STATE, "%s-%s.json" % (name, safe))


@contextlib.contextmanager
def _lock(name="state", wait=3.0):
    """A small cross-process lock: agents can run several tool calls, and so several hooks, at once."""
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    path = os.path.join(STATE, ".%s.lock" % name)
    deadline = time.time() + wait
    held = False
    while not held:
        try:
            os.mkdir(path)
            held = True
        except OSError:
            try:
                if time.time() - os.path.getmtime(path) > 10:
                    os.rmdir(path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                break  # go ahead unlocked rather than stall the agent
            time.sleep(0.02)
    try:
        yield
    finally:
        if held:
            try:
                os.rmdir(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------------------------
# Reading the event


def find_rules(cwd):
    """The rules in force for a folder: the nearest .claude/scopeblind-rules.json above it, else ~/.scopeblind/rules.json."""
    probe = os.path.abspath(cwd or os.getcwd())
    while True:
        cand = os.path.join(probe, ".claude", "scopeblind-rules.json")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    user = os.path.expanduser("~/.scopeblind/rules.json")
    return user if os.path.isfile(user) else None


def normalize_event(event, agent):
    """The fields the rules need, from a Claude Code or Codex PreToolUse payload."""
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = None
    if tool in SHELL_TOOLS:
        command = inp.get("command") or inp.get("cmd")
        if isinstance(command, list):
            command = command[2] if len(command) >= 3 and command[1] in ("-c", "-lc") else " ".join(map(str, command))
        tool = "Bash"
    target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or inp.get("url")
    if tool == "apply_patch":
        patch = inp.get("input") or inp.get("patch") or inp.get("command") or ""
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch if isinstance(patch, str) else "", re.M)
        target = ", ".join(p.strip() for p in paths) or target
    return {"tool": tool, "command": command if isinstance(command, str) else None, "target": target,
            "session": event.get("session_id") or event.get("thread_id"), "cwd": event.get("cwd") or os.getcwd(),
            "id": event.get("tool_use_id") or event.get("call_id"), "transcript": event.get("transcript_path"),
            "agent": agent, "description": inp.get("description") if isinstance(inp.get("description"), str) else None}


def guard(ev, rules_path):
    """The hook's own protections: its state is off limits, and rule, settings or approval changes wait for the person."""
    text = (ev["command"] or "") + " " + (ev["target"] or "")
    home = os.path.expanduser("~")

    def mentions(path):
        return bool(path) and (path in text or path.replace(home, "~") in text or path.replace(home, "$HOME") in text)
    if mentions(STATE) or ".scopeblind/replay/state" in text:
        return "deny", "guard-state", ("The replay-and-discover hook's state (including the standard page's write "
                                       "token) is used only by the hook. Do not read or change it; ask the person if "
                                       "something looks wrong.")
    if ev["command"] and re.search(r"replay\.py[\"']?\s+approve\b", ev["command"]) and not re.search(r"\s--list\b", ev["command"]):
        if ev["agent"] == "codex":
            return "deny", "guard-approve", ("Only the person can approve, from their own terminal. Stop and ask them "
                                             "to run this command themselves if they agree.")
        return "ask", "guard-approve", ("The agent is asking you to approve in advance: %s. Approve only if you want "
                                        "this." % redact(ev["command"], 200))
    target = ev["target"] and os.path.abspath(os.path.join(ev["cwd"], ev["target"]))
    if rules_path and target == os.path.abspath(rules_path):
        return "ask", "guard-rules", "This changes the rules your agents follow. Approve only if you asked for this change."
    if ev["command"] and rules_path and mentions(rules_path) and re.search(
            r"(?:>|\btee\b|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|write|json\.dump|replay\.py\s+(?:label|enable|disable))",
            ev["command"]):
        return "ask", "guard-rules", "This command may change the rules your agents follow. Approve only if you asked for it."
    if (ev["target"] and _SETTINGS.search(ev["target"])) or (ev["command"] and re.search(
            r"replay\.py[\"']?\s+(?:uninstall|install|disable)\b", ev["command"]) and "--dry-run" not in ev["command"]):
        return "ask", "guard-settings", "This changes agent settings or hooks, which can turn the rules on or off."
    return None


def _needs_history(rule):
    t = rule.get("type")
    return t == "require_before" or (t == "limit" and rule.get("per") == "session") or (
        t == "hold" and int(rule.get("approval_minutes", 0) or 0) > 0)


def _prior(ev, extra):
    path = ev["transcript"]
    if not path or not os.path.isfile(path):
        return []
    if ev["agent"] == "codex" or "/.codex/" in path:
        acts = list(read_codex_file(path, extra=extra, tail_bytes=TAIL_BYTES))
    else:
        acts = list(read_claude_file(path, extra=extra, tail_bytes=TAIL_BYTES))
    acts = [a for a in acts if a.id != ev["id"]]
    acts.sort(key=lambda a: a.ts or 0)
    return acts


def _checked_steps(doc):
    return {s for r in doc.get("rules") or [] if r.get("type") == "require_before" and r.get("fresh")
            for s in r.get("requires") or []}


def _fingerprint_callback(checks):
    now_cache = {}

    def compare(b, action, m, ignore):
        rec = checks.get(b.id or "")
        if not rec:
            return None
        segs = rules.action_plan(action)
        where = segs[m].dir if segs and m < len(segs) and segs[m].dir else action.project
        root = fingerprint.repo_root(where)
        if not root:
            return None, "not a git working copy"
        then = next((s for s in rec.get("snaps") or [] if s.get("root") == root), None)
        if not then:
            return None, "the check ran in another folder"
        if root not in now_cache:
            now_cache[root] = fingerprint.snapshot(root)
        if now_cache[root] is None:
            return None, "the working copy could not be read"
        kinds = set(segs[m].steps) if segs and m < len(segs) else set(action.steps)
        mode = "head" if kinds & rules.PUSH_LIKE else "worktree"
        return fingerprint.compare(then, now_cache[root], mode, ignore)
    return compare


# ---------------------------------------------------------------------------------------------------------------
# The standard's page on scopeblind.com


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _http(url, body=None, token=None, timeout=6):
    headers = {"Accept": "application/json", "User-Agent": "replay-and-discover"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, data=data, headers=headers, method="POST" if data else "GET"), timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}


def page_hold(rule, ev, now, grants_path):
    """Hold an action on the standard's page. Returns (decision, reason, info) like rules.decide."""
    page = _read_json(_state("page"), None)
    says = rule.get("says") or "A person approves this."
    if not page or not page.get("report") or not page.get("token"):
        return "deny", ("%s It waits for the person named on the standard's page, but no page is connected on this "
                        "machine. Stop and ask the person to connect one (replay.py connect)." % says), {}
    report = page["report"]
    sid = (parse_qs(urlparse(report).query).get("s") or [""])[0]
    subject = ev["command"] or ev["target"] or ev["tool"]
    payload = {"tool": ev["tool"], "subject": subject, "session": ev["session"], "day": now.strftime("%Y-%m-%d")}
    payload_hash = _sha256(_canonical(payload))
    hid = _sha256("scopeblind.held_action.v1\0%s\0%s\0%s" % (sid, ev["tool"], payload_hash))[:24]
    link = report.replace("/api/standard?", "/standard?") + "#held-" + hid
    info = {"page_hid": hid}
    try:
        status, got = _http(report + "&held=" + hid)
        if status == 404:
            wanted = (rule.get("match") or {}).get("steps") or []
            steps = [s for s in ev.get("steps") or [] if s in wanted] or [ev["tool"]]
            summary = "%s: %s" % (", ".join(label(s) for s in steps), redact(subject, 300))
            status, got = _http(report + "&op=held", {
                "sid": sid, "hid": hid, "request_id": (ev["session"] or "")[:80], "tool": ev["tool"],
                "readback": {"summary": summary, "payload_hash": payload_hash, "amount": None, "currency": None},
                "reason": says[:300], "run_id": (ev["session"] or "")[:64]}, token=page["token"])
            if status != 200 or not got.get("ok"):
                return "deny", "%s The standard's page did not accept the request (%s). The action waits." % (
                    says, got.get("error") or status), info
            return "deny", ("%s It is waiting for the person named on the standard to decide at %s. Carry on with "
                            "other work, and retry this exact command after they decide. A changed command is a new "
                            "request." % (says, got.get("url") or link)), info
        if status != 200:
            return "deny", "%s The standard's page answered %s. The action waits." % (says, got.get("error") or status), info
    except (OSError, URLError, ValueError) as e:
        return "deny", "%s The standard's page could not be reached (%s). The action waits; retry later." % (says, e), info
    held = got.get("held") or {}
    decision = held.get("decision") or None
    if not decision:
        return "deny", "%s Still waiting for the person to decide at %s. Retry this exact command after they do." % (
            says, link), info
    if decision.get("hid") != hid or decision.get("payload_hash") != payload_hash:
        return "deny", "%s The decision on the page does not match this exact action. The action waits." % says, info
    who = ((decision.get("approver") or {}).get("name") or (decision.get("approver") or {}).get("label")
           or "the person named on the standard")
    if decision.get("decision") == "approve":
        span = int(rule.get("approval_minutes", 0) or 0) * 60
        if span:
            with _lock("grants"):
                grants = _read_json(grants_path, [])
                grants.append({"id": "page-" + hid, "rule": rule.get("id"), "session": ev["session"], "at": now.timestamp(),
                               "until": now.timestamp() + span, "by": who, "via": "page"})
                _private_write(grants_path, json.dumps(grants[-200:]))
        return None, None, dict(info, covered_by="page:" + hid, approver=who)
    return "deny", "%s %s declined this on the standard's page. Do not retry it; tell the person." % (says, who), info


# ---------------------------------------------------------------------------------------------------------------
# The decision


def run(event, rules_path, log_path, agent="claude-code", require_live=False):
    if event.get("hook_event_name") not in (None, "PreToolUse"):
        return None
    ev = normalize_event(event, agent)
    rules_path = rules_path or find_rules(ev["cwd"])
    if not rules_path:
        return None
    doc = rules.load(rules_path)
    if rules.problems(doc) or doc.get("live") is False or (require_live and doc.get("live") is not True):
        return None
    now = datetime.now(timezone.utc)
    decision, rule_id, reason, info = None, None, None, {}
    steps, segs = tool_plan(ev["tool"], ev["command"], doc.get("_extra"), ev["cwd"])
    action = Action(id=ev["id"], agent=agent, session=ev["session"], subagent=False, project=ev["cwd"],
                    ts=now.timestamp(), tool=ev["tool"], command=ev["command"], target=ev["target"], steps=steps)
    action.cache["plan"] = segs
    ev["steps"] = steps
    checked = _checked_steps(doc) & set(action.steps)
    guarded = guard(ev, rules_path)
    grants_path = _state("grants")
    if guarded:
        decision, rule_id, reason = guarded
    else:
        live = [r for r in doc.get("rules") or [] if r.get("type") in rules.LIVE and rules.matches(r, action)]
        if not live and not checked:
            return None
        prior = _prior(ev, doc.get("_extra")) if any(_needs_history(r) for r in live) else []
        with _lock():
            checks = _read_json(_state("checks", ev["session"]), {})
            asked = _read_json(_state("asks", ev["session"]), [])
        grants = _read_json(grants_path, [])
        counts_path = os.path.join(STATE, "limits-%s.json" % now.strftime("%Y-%m-%d"))
        decision, rule, reason, info = rules.decide(doc, action, prior, _read_json(counts_path, {}).get(doc.get("name"), {}),
                                                    _fingerprint_callback(checks), asked, grants)
        rule_id = rule.get("id") if rule else None
        if decision == "ask" and rule and rule.get("approver") == "page":
            decision, reason, info = page_hold(rule, ev, now, grants_path)
        elif decision == "ask" and agent == "codex":
            span = max(int(rule.get("approval_minutes", 0) or 0), 30) if rule else 30
            reason = ("%s Codex cannot ask for approval here, so this is refused for now. Stop and ask the person. If "
                      "they agree, they can approve from their own terminal: python3 \"%s\" approve %s --minutes %d "
                      "--session %s  Then retry." % (reason, REPLAY, rule_id, span, ev["session"]))
            decision = "deny"
        if decision != "deny":
            with _lock():
                counts_all = _read_json(counts_path, {})
                counts = counts_all.get(doc.get("name"), {})
                changed = False
                for r in doc.get("rules") or []:
                    if r.get("type") == "limit" and r.get("per", "day") == "day" and rules.matches(r, action):
                        counts[r.get("id")] = counts.get(r.get("id"), 0) + 1
                        changed = True
                if changed:
                    counts_all[doc.get("name")] = counts
                    _private_write(counts_path, json.dumps(counts_all))
            if checked and ev["id"]:
                roots = []
                for seg in segs:
                    if set(seg.steps) & checked:
                        root = fingerprint.repo_root(seg.dir or ev["cwd"])
                        if root and root not in roots:
                            roots.append(root)
                snaps = [s for s in (fingerprint.snapshot(r) for r in roots) if s]
                if snaps:
                    with _lock():
                        checks = _read_json(_state("checks", ev["session"]), {})
                        checks[ev["id"]] = {"ts": now.timestamp(), "snaps": snaps}
                        for old in sorted(checks, key=lambda k: checks[k].get("ts", 0))[:-MAX_CHECKS]:
                            del checks[old]
                        _private_write(_state("checks", ev["session"]), json.dumps(checks))
        if decision == "ask" and ev["id"]:
            with _lock():
                asked = _read_json(_state("asks", ev["session"]), [])
                asked.append({"rule": rule_id, "tool_use_id": ev["id"], "ts": now.timestamp()})
                _private_write(_state("asks", ev["session"]), json.dumps(asked[-200:]))
    if not decision and not info.get("covered_by"):
        return None
    _private_write(log_path, json.dumps({
        "at": now.isoformat(timespec="seconds"), "ts": round(now.timestamp(), 3), "agent": agent,
        "session": ev["session"], "tool_use_id": ev["id"], "tool": ev["tool"], "steps": action.steps,
        "rule": rule_id, "decision": decision or "covered", "reason": reason, "covered_by": info.get("covered_by"),
        "approver": info.get("approver"), "page_hid": info.get("page_hid"),
        "command": redact(ev["command"] or ev["target"] or "", 200), "project": redact(ev["cwd"], 120),
        "rules_file": os.path.abspath(rules_path)}) + "\n", append=True)
    if not decision:
        return None
    text = "ScopeBlind rule '%s': %s" % (rule_id, reason)
    if agent == "codex":
        text = text.rstrip(". ")  # Codex adds ". Command: ..." after the reason
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": text}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default=os.environ.get("SCOPEBLIND_RULES"),
                    help="rules file (default: the nearest .claude/scopeblind-rules.json, else ~/.scopeblind/rules.json)")
    ap.add_argument("--log", default=os.path.join(HOME, "decisions.jsonl"))
    ap.add_argument("--agent", default="claude-code", choices=("claude-code", "codex"))
    ap.add_argument("--require-live", action="store_true", help="only enforce rules files that say \"live\": true")
    args = ap.parse_args()
    started = time.time()
    try:
        raw = sys.stdin.read()
        if args.rules and not os.path.isfile(args.rules):
            return 0
        out = run(json.loads(raw or "{}"), args.rules, args.log, args.agent, args.require_live)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as e:  # never break the session: no decision means normal permissions apply
        sys.stderr.write("replay-and-discover hook skipped (%.2fs): %s\n" % (time.time() - started, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
