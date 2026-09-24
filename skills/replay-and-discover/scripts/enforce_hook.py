#!/usr/bin/env python3
"""Claude Code PreToolUse hook: apply a replay-and-discover rules file before each tool call.

Install (after the person agrees), in ~/.claude/settings.json or <project>/.claude/settings.json:

    {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
      "command": "python3 /ABSOLUTE/PATH/enforce_hook.py --rules /ABSOLUTE/PATH/scopeblind-rules.json"}]}]}}

Decisions: block -> deny; hold -> ask (the person approves in Claude Code); require_before with a missing step ->
ask or deny as the rule says; a limit already reached -> deny. require_after rules are never enforced live (nothing
can prevent a step that has not happened yet); rehearse them with replay.py instead.

If anything goes wrong (unreadable rules, unexpected input), the hook makes no decision, so Claude Code's normal
permission rules apply. Every decision it does make is appended to ~/.scopeblind/replay/decisions.jsonl.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sbreplay import rules  # noqa: E402
from sbreplay.classify import tool_steps  # noqa: E402
from sbreplay.redact import redact  # noqa: E402
from sbreplay.sources import Action, read_claude_file  # noqa: E402

STATE_DIR = os.path.expanduser("~/.scopeblind/replay")
TAIL_BYTES = 8 * 1024 * 1024


def _append_private(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def _day_counts(doc_name, day):
    path = os.path.join(STATE_DIR, "state", "limits-%s.json" % day)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return path, json.load(fh).get(doc_name, {})
    except (OSError, ValueError):
        return path, {}


def _save_day_counts(path, doc_name, counts):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            all_counts = json.load(fh)
    except (OSError, ValueError):
        all_counts = {}
    all_counts[doc_name] = counts
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(all_counts, fh)


def run(event, rules_path, log_path):
    if event.get("hook_event_name") not in (None, "PreToolUse"):
        return None
    doc = rules.load(rules_path)
    if rules.problems(doc):
        return None
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = inp.get("command") if tool == "Bash" else None
    target = inp.get("file_path") or inp.get("notebook_path") or inp.get("url")
    now = datetime.now(timezone.utc)
    action = Action(id=event.get("tool_use_id"), agent="claude-code", session=event.get("session_id"), subagent=False,
                    project=event.get("cwd"), ts=now.timestamp(), tool=tool, command=command, target=target,
                    steps=tool_steps(tool, command, doc.get("_extra")))
    prior = []
    tpath = event.get("transcript_path")
    if tpath and os.path.isfile(tpath):
        prior = [a for a in read_claude_file(tpath, extra=doc.get("_extra"), tail_bytes=TAIL_BYTES)
                 if a.id != action.id]
        prior.sort(key=lambda a: a.ts or 0)
    day = now.strftime("%Y-%m-%d")
    state_path, counts = _day_counts(doc.get("name"), day)
    decision, rule, reason = rules.decide(doc, action, prior, counts)
    if decision != "deny":
        changed = False
        for r in doc.get("rules") or []:
            if r.get("type") == "limit" and r.get("per", "day") == "day" and rules.matches(r, action):
                counts[r.get("id")] = counts.get(r.get("id"), 0) + 1
                changed = True
        if changed:
            _save_day_counts(state_path, doc.get("name"), counts)
    if not decision:
        return None
    _append_private(log_path, {"at": now.isoformat(timespec="seconds"), "session": event.get("session_id"),
                               "tool": tool, "steps": action.steps, "rule": rule.get("id"), "decision": decision,
                               "reason": reason, "command": redact(command or target or "", 200),
                               "rules_file": os.path.abspath(rules_path)})
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": "ScopeBlind rule '%s': %s" % (rule.get("id"), reason),
    }}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default=os.environ.get("SCOPEBLIND_RULES"))
    ap.add_argument("--log", default=os.path.join(STATE_DIR, "decisions.jsonl"))
    args = ap.parse_args()
    try:
        if not args.rules or not os.path.isfile(args.rules):
            return 0
        event = json.loads(sys.stdin.read() or "{}")
        out = run(event, args.rules, args.log)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as e:  # never break the session: no decision means normal permissions apply
        sys.stderr.write("replay-and-discover hook skipped: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
