"""Deterministic rules, evaluated the same way over past history (rehearsal) and before a live tool call (hook).

A rules file is JSON:

    {
      "version": 1,
      "name": "My definition of done",
      "steps": {"live_check": ["curl .*https://example\\.com"]},
      "rules": [
        {"id": "approve-releases", "type": "hold", "says": "A person approves every deploy and publish.",
         "match": {"steps": ["deploy", "publish"]}},
        {"id": "tests-before-push", "type": "require_before", "says": "Tests pass on the final version before a push.",
         "match": {"steps": ["push"]}, "requires": ["test"], "passed": true, "fresh": true, "if_missing": "hold"}
      ]
    }

Types:
- hold: a person approves before the action runs (live: "ask").
- block: the action never runs (live: "deny").
- require_before: every step in `requires` ran earlier in the session (within `within_minutes`, default 180);
  `passed` means its last run succeeded; `fresh` means it ran after the last file edit. `if_missing` is hold or block.
- require_after: at least one step in `requires` follows within `within_minutes` (default 30). Rehearsal reports
  misses; a live hook cannot prevent something that has not happened yet, so it is never enforced live.
- limit: at most `max` matching actions per `per` ("session" or "day"); beyond that, blocked.
- unsupported: a sentence the person wants that no deterministic check covers. It is listed, never enforced.

`match` fields (all present fields must hold): steps (any of), command (regex), tool (regex), path (glob on the file
an edit touches), secrets (true: the command carries a literal secret), branch (regex). `scope` narrows a rule to
projects (substrings of the working directory) and agents ("claude-code", "codex").
"""
import fnmatch
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .classify import STEPS, compile_extra, focus, label, lc, noun, split_commands, strip_heredocs
from .redact import find_secrets

TYPES = ("hold", "block", "require_before", "require_after", "limit", "unsupported")
LIVE = ("hold", "block", "require_before", "limit")


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return normalize(doc)


def normalize(doc):
    doc = dict(doc or {})
    doc.setdefault("version", 1)
    doc.setdefault("name", "Untitled rules")
    doc.setdefault("rules", [])
    doc["_extra"] = compile_extra(doc.get("steps"))
    return doc


def problems(doc):
    """Human-readable problems with a rules document (empty list if it is sound)."""
    out = []
    known = set(STEPS) | set((doc.get("steps") or {}).keys())
    seen = set()
    for i, r in enumerate(doc.get("rules") or []):
        rid = r.get("id") or "#%d" % (i + 1)
        if rid in seen:
            out.append("%s: duplicate id" % rid)
        seen.add(rid)
        if r.get("type") not in TYPES:
            out.append("%s: type must be one of %s" % (rid, ", ".join(TYPES)))
        if not r.get("says"):
            out.append("%s: add a plain-English 'says' sentence" % rid)
        m = r.get("match") or {}
        if r.get("type") != "unsupported" and not any(k in m for k in ("steps", "command", "tool", "path", "secrets", "branch")):
            out.append("%s: 'match' needs at least one of steps, command, tool, path, secrets, branch" % rid)
        for k in (m.get("steps") or []) + (r.get("requires") or []):
            if k not in known:
                out.append("%s: unknown step '%s'" % (rid, k))
        for key in ("command", "tool", "branch"):
            if key in m:
                try:
                    re.compile(m[key])
                except re.error as e:
                    out.append("%s: %s is not a valid pattern (%s)" % (rid, key, e))
        if r.get("type") in ("require_before", "require_after") and not r.get("requires"):
            out.append("%s: %s needs 'requires'" % (rid, r.get("type")))
        if r.get("type") == "limit" and not isinstance(r.get("max"), int):
            out.append("%s: limit needs an integer 'max'" % rid)
    return out


def _in_scope(rule, action):
    sc = rule.get("scope") or {}
    projects = sc.get("projects")
    if projects and not any(p == "*" or (action.project and p.lower() in action.project.lower()) for p in projects):
        return False
    agents = sc.get("agents")
    if agents and action.agent not in agents:
        return False
    return True


def matches(rule, action):
    if rule.get("type") == "unsupported" or not _in_scope(rule, action):
        return False
    m = rule.get("match") or {}
    criteria = False
    if "steps" in m:
        criteria = True
        if not set(m["steps"]) & set(action.steps):
            return False
    if "command" in m:
        criteria = True
        if not action.command:
            return False
        # With steps, the pattern applies to the part of the command that performed them, so a force-push to a
        # feature branch does not match "main" just because main appears elsewhere in the same command line.
        if "steps" in m:
            parts = [seg for seg in split_commands(strip_heredocs(action.command)) if focus(seg, m["steps"])]
        else:
            parts = [action.command]
        if not any(re.search(m["command"], part, re.I) for part in parts):
            return False
    if "tool" in m:
        criteria = True
        if not re.search(m["tool"], action.tool or "", re.I):
            return False
    if "path" in m:
        criteria = True
        target = action.target or ""
        if not any(fnmatch.fnmatch(t.strip(), m["path"]) for t in target.split(",")):
            return False
    if m.get("secrets"):
        criteria = True
        if not find_secrets(action.command):
            return False
    if "branch" in m:
        criteria = True
        if not (action.branch and re.search(m["branch"], action.branch)):
            return False
    return criteria


def _matched_steps(rule, action):
    wanted = set((rule.get("match") or {}).get("steps") or [])
    return [i for i, s in enumerate(action.steps) if s in wanted] or [len(action.steps)]


def check_before(rule, prior, action):
    """('ok' | 'missing' | 'cant_tell', detail) for a require_before rule, given the session's earlier actions."""
    window = float(rule.get("within_minutes", 180)) * 60
    need_pass = rule.get("passed", True)
    fresh = rule.get("fresh", False)
    first_match = min(_matched_steps(rule, action))
    in_command = set(action.steps[:first_match])
    recent = [b for b in prior if b.ran() and (action.ts is None or b.ts is None or action.ts - b.ts <= window)]
    last_edit = max((i for i, b in enumerate(recent) if "edit" in b.steps), default=None)
    missing, unknown = [], []
    required = rule.get("requires") or []
    for req in required:
        if req in in_command:
            continue
        idx = max((i for i, b in enumerate(recent) if req in b.steps), default=None)
        if idx is None:
            missing.append("%s did not run" % noun(req))
            continue
        if fresh and last_edit is not None and idx < last_edit:
            missing.append("files changed after %s last ran" % noun(req))
            continue
        if need_pass:
            outcome = recent[idx].outcome
            if outcome == "ok":
                continue
            if outcome in ("unknown", "error", "interrupted"):
                unknown.append("%s ran but the result was not recorded" % noun(req))
            else:
                missing.append("%s failed the last time" % noun(req))
    if missing:
        return "missing", "; ".join(missing)
    if unknown:
        return "cant_tell", "; ".join(unknown)
    return "ok", ""


def check_after(rule, later, action):
    window = float(rule.get("within_minutes", 30)) * 60
    wanted = set(rule.get("requires") or [])
    for b in later:
        if action.ts is not None and b.ts is not None and b.ts - action.ts > window:
            break
        if wanted & set(b.steps) and b.ran():
            return "ok", ""
    return "missing", "no %s within %d minutes" % (" or ".join(noun(r) for r in sorted(wanted)), window // 60)


def _day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def rehearse(doc, actions, days=None, max_examples=5):
    """What each rule would have done over the history, if it had been on."""
    from .discover import example, sessions_of
    rules = [r for r in doc.get("rules") or [] if r.get("type") in TYPES]
    res = {}
    for r in rules:
        res[r.get("id")] = {"id": r.get("id"), "says": r.get("says"), "type": r.get("type"), "matched": 0,
                            "already_refused": 0, "would_hold": 0, "would_block": 0, "satisfied": 0, "violations": 0,
                            "cant_tell": 0, "examples": [], "enforced_live": r.get("type") in LIVE}
    for key, acts in sessions_of(actions).items():
        counts = Counter()
        lo = defaultdict(int)
        for i, a in enumerate(acts):
            for r in rules:
                if not matches(r, a):
                    continue
                if r.get("type") == "require_before" and a.ts is not None:
                    edge = a.ts - float(r.get("within_minutes", 180)) * 60
                    rid = r.get("id")
                    while lo[rid] < i and acts[lo[rid]].ts is not None and acts[lo[rid]].ts < edge:
                        lo[rid] += 1
                out = res[r.get("id")]
                out["matched"] += 1
                if not a.ran():
                    out["already_refused"] += 1
                t = r.get("type")
                why = None
                if t == "hold":
                    out["would_hold"] += 1
                    why = "would wait for a person"
                elif t == "block":
                    out["would_block"] += 1
                    why = "would be refused"
                elif t == "require_before":
                    status, detail = check_before(r, acts[lo[r.get("id")]:i], a)
                    if status == "ok":
                        out["satisfied"] += 1
                    elif status == "cant_tell":
                        out["cant_tell"] += 1
                    else:
                        out["violations"] += 1
                        out["would_block" if r.get("if_missing") == "block" else "would_hold"] += 1
                        why = detail
                elif t == "require_after":
                    status, detail = check_after(r, acts[i + 1:], a)
                    if status == "ok":
                        out["satisfied"] += 1
                    else:
                        out["violations"] += 1
                        why = detail
                elif t == "limit":
                    k = a.session_key if r.get("per") == "session" else _day(a.ts)
                    if a.ran():
                        counts[(r.get("id"), k)] += 1
                    if counts[(r.get("id"), k)] > int(r.get("max", 0)):
                        out["violations"] += 1
                        out["would_block"] += 1
                        why = "over the limit of %s per %s" % (r.get("max"), r.get("per", "day"))
                    else:
                        out["satisfied"] += 1
                if why and len(out["examples"]) < max_examples:
                    m = r.get("match") or {}
                    ex = example(a, m.get("steps"), secret=bool(m.get("secrets")))
                    ex["why"] = why
                    out["examples"].append(ex)
    for out in res.values():
        if days:
            out["holds_per_week"] = round(out["would_hold"] * 7.0 / days, 1)
    unsupported = [{"id": r.get("id"), "says": r.get("says"), "note": r.get("note")}
                   for r in doc.get("rules") or [] if r.get("type") == "unsupported"]
    return {"name": doc.get("name"), "rules": list(res.values()), "unsupported": unsupported}


def decide(doc, action, prior, day_counts=None):
    """Live decision for one tool call. Returns (decision, rule, reason) with decision 'deny', 'ask' or None."""
    day_counts = day_counts or {}
    best = (None, None, None)
    rank = {"deny": 2, "ask": 1, None: 0}
    for r in doc.get("rules") or []:
        t = r.get("type")
        if t not in LIVE or not matches(r, action):
            continue
        decision, reason = None, None
        if t == "block":
            decision, reason = "deny", r.get("says")
        elif t == "hold":
            decision, reason = "ask", r.get("says")
        elif t == "require_before":
            status, detail = check_before(r, prior, action)
            if status == "missing":
                decision = "deny" if r.get("if_missing") == "block" else "ask"
                reason = "%s (%s)" % (r.get("says"), detail)
            elif status == "cant_tell":
                decision, reason = "ask", "%s (%s)" % (r.get("says"), detail)
        elif t == "limit":
            used = day_counts.get(r.get("id"), 0)
            if r.get("per") == "session":
                used = sum(1 for b in prior if b.ran() and matches(r, b))
            if used >= int(r.get("max", 0)):
                decision, reason = "deny", "%s (already %d)" % (r.get("says"), used)
        if rank[decision] > rank[best[0]]:
            best = (decision, r, reason)
    return best


def readback(doc):
    """Each rule restated with how it is checked, whether a live hook enforces it, and what it does not cover."""
    rows = []
    for r in doc.get("rules") or []:
        t = r.get("type")
        m = r.get("match") or {}
        parts = []
        if m.get("steps"):
            parts.append("when the agent is about to " + " or ".join(lc(label(s)) for s in m["steps"]))
        if m.get("command"):
            parts.append("and the command matches /%s/" % m["command"])
        if m.get("tool"):
            parts.append("for tools matching /%s/" % m["tool"])
        if m.get("path"):
            parts.append("for files matching %s" % m["path"])
        if m.get("secrets"):
            parts.append("when a command carries a literal secret")
        if m.get("branch"):
            parts.append("on branches matching /%s/" % m["branch"])
        when = " ".join(parts) or "(no match set)"
        if t == "hold":
            how = "Held for a person " + when + "."
        elif t == "block":
            how = "Refused " + when + "."
        elif t == "require_before":
            how = "%s %s unless %s ran earlier in the session%s%s." % (
                "Refused" if r.get("if_missing") == "block" else "Held for a person", when,
                " and ".join(noun(x) for x in r.get("requires") or []),
                " and passed" if r.get("passed", True) else "", " after the last file change" if r.get("fresh") else "")
        elif t == "require_after":
            how = "Reported (never prevented) if no %s follows within %s minutes, %s." % (
                " or ".join(noun(x) for x in r.get("requires") or []), r.get("within_minutes", 30), when)
        elif t == "limit":
            how = "Refused beyond %s per %s, %s." % (r.get("max"), r.get("per", "day"), when)
        else:
            how = "Not checked: no deterministic check covers this sentence."
        rows.append({"id": r.get("id"), "says": r.get("says"), "how": how, "type": t,
                     "enforced_live": t in LIVE, "checked_in_replay": t != "unsupported"})
    return rows
