"""Deterministic rules, evaluated the same way over past history (rehearsal) and before a live tool call (hook).

A rules file is JSON:

    {
      "version": 1,
      "name": "My definition of done",
      "steps": {"live_check": ["curl .*https://example\\.com"]},
      "budget": {"asks_per_week": 10},
      "rules": [
        {"id": "approve-releases", "type": "hold", "says": "A person approves deploys and publishes.",
         "match": {"steps": ["deploy", "publish"]}, "approval_minutes": 60},
        {"id": "tests-before-push", "type": "require_before", "says": "Tests pass on the final version before a push.",
         "match": {"steps": ["push"]}, "requires": ["test"], "passed": true, "fresh": true, "if_missing": "fix"}
      ]
    }

Types:
- hold: a person approves before the action runs (live: Claude Code asks them). With `approval_minutes`, one
  approval covers the rule's other matching actions in the same session for that long.
- block: the action never runs (live: refused, and the agent is told not to retry).
- require_before: every step in `requires` ran earlier (within `within_minutes`, default 180) or earlier in the same
  command joined so that its failure stops the action. `passed` (default true): it succeeded. `fresh`: it saw the
  final version (live, compared by content; in replay, by the edits and file-changing commands in between).
  `if_missing`: "fix" (default: refuse and tell the agent exactly what to run first), "hold" (ask a person) or
  "block". `ignore`: file patterns (such as "dist/**") that do not count as changes.
- require_after: at least one step in `requires` follows within `within_minutes` (default 30). Rehearsal reports
  misses; a live hook cannot prevent something that has not happened yet, so it is never enforced live.
- limit: at most `max` matching actions per `per` ("session" or "day"); beyond that, refused.
- unsupported: a sentence the person wants that no deterministic check covers. It is listed, never enforced.

`match` fields (all present fields must hold): steps (any of), command (regex), tool (regex), path (glob on the file
an edit touches), secrets (true: the command carries a literal secret), branch (regex). `scope` narrows a rule to
projects (substrings of the working directory) and agents ("claude-code", "codex").
"""
import fnmatch
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .classify import (STEPS, compile_extra, gated, label, lc, noun, gerund, plan, real_path, repo_of, writes,
                       SHELL_TOOLS)
from .fingerprint import repo_root
from .redact import find_secrets, redact

TYPES = ("hold", "block", "require_before", "require_after", "limit", "unsupported")
LIVE = ("hold", "block", "require_before", "limit")
IF_MISSING = ("fix", "hold", "block")
DEFAULT_BUDGET = 10
PUSH_LIKE = {"push", "pr_open", "pr_merge", "force_push"}


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
    for kind, pats in (doc.get("steps") or {}).items():
        for p in pats if isinstance(pats, list) else [pats]:
            try:
                re.compile(p)
            except (re.error, TypeError) as e:
                out.append("steps.%s: %r is not a valid pattern (%s)" % (kind, p, e))
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
        if r.get("type") == "require_before" and r.get("if_missing", "fix") not in IF_MISSING:
            out.append("%s: if_missing must be one of %s" % (rid, ", ".join(IF_MISSING)))
        if r.get("type") == "limit" and not isinstance(r.get("max"), int):
            out.append("%s: limit needs an integer 'max'" % rid)
        if "approval_minutes" in r and not (isinstance(r["approval_minutes"], int) and r["approval_minutes"] >= 0):
            out.append("%s: approval_minutes must be a whole number of minutes (0: every call)" % rid)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Reading actions


def action_plan(action):
    """The parsed parts of an action's shell command (cached on the action)."""
    if "plan" not in action.cache:
        action.cache["plan"] = plan(action.command, None, action.project) if action.command else []
    return action.cache["plan"]


def _seg_index(action, kinds):
    """Index of the first part of the command that performed one of `kinds` (len(parts) for non-shell tools)."""
    segs = action_plan(action)
    for i, seg in enumerate(segs):
        if set(seg.steps) & set(kinds):
            return i
    return len(segs)


def step_result(action, r):
    """'pass', 'fail' or None (not recorded) for part `r` of an action's command."""
    segs = action_plan(action)
    if not segs:
        return {"ok": "pass", "failed": "fail"}.get(action.outcome)
    if r is None or r >= len(segs):
        return None
    if action.outcome == "ok" and gated(segs, r):
        return "pass"
    if action.outcome == "failed" and r == len(segs) - 1 and segs[r].before != "|" and segs[r].group == 0:
        return "fail"
    return action.verdict


def _strongest(values):
    values = list(values)
    return "yes" if "yes" in values else ("maybe" if "maybe" in values else None)


def _root_for(action, m=None):
    segs = action_plan(action)
    where = segs[m].dir if (segs and m is not None and m < len(segs) and segs[m].dir) else action.project
    where = real_path(where) if where else where
    return repo_of(where) or where


def action_writes(action, root, upto=None):
    """Whether an action changed files under `root`: 'yes', 'maybe' or None. `upto`: only its first parts."""
    key = ("writes", root, upto)
    if key in action.cache:
        return action.cache[key]
    result = None
    if action.tool in SHELL_TOOLS and action.command:
        segs = action_plan(action)
        result = _strongest(writes(seg, root, action.command) for seg in segs[:upto])
    elif "edit" in action.steps:
        targets = [t.strip() for t in (action.target or "").split(",") if t.strip()]
        if not targets:
            result = "maybe"
        else:
            found = []
            for t in targets:
                path = real_path(os.path.normpath(t if os.path.isabs(t) else os.path.join(action.project or "", t)))
                found.append("yes" if (root is None or path == root or path.startswith(root.rstrip("/") + "/")) else None)
            result = _strongest(found)
    action.cache[key] = result
    return result


# ---------------------------------------------------------------------------------------------------------------
# Matching


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
            parts = [seg.raw for seg in action_plan(action) if set(seg.steps) & set(m["steps"])]
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


# ---------------------------------------------------------------------------------------------------------------
# require_before


def _last_command_for(prior, req):
    for b in reversed(prior):
        if req in b.steps and b.command:
            segs = action_plan(b)
            for seg in segs:
                if req in seg.steps:
                    return redact(seg.core or seg.raw, 80)
    return None


def evaluate_requirement(req, prior, action, m, need_pass=True, fresh=False, window=180 * 60, fingerprint=None,
                         ignore=(), others=None):
    """Did step `req` happen before part `m` of `action`, succeed, and see the final version?

    Returns (status, code, info): status "ok", "missing" or "cant_tell"; code names the reason; info carries what a
    message needs (the command that ran, the files that changed, the join that let a failure through).
    `others(root, since, until, session)`, if given, returns other sessions' actions in that window, so an edit by
    another agent working in the same folder counts too."""
    segs = action_plan(action)
    root = _root_for(action, m)
    info = {"req": req}
    same = [i for i in range(min(m, len(segs))) if req in segs[i].steps]
    if same:
        r = same[-1]
        info["command"] = redact(segs[r].core or segs[r].raw, 80)
        if gated(segs, r, m):
            if fresh:
                w = _strongest(writes(segs[k], root, action.command) for k in range(r + 1, m))
                if w == "yes":
                    return "missing", "changed_in_command", info
                if w == "maybe":
                    return "cant_tell", "maybe_changed_in_command", info
            return "ok", "same_command", info
        info["join"] = segs[r].after if segs[r].after != "|" or segs[r].pipefail else "| (without pipefail)"
    recent = [b for b in prior if b.ran() and (action.ts is None or b.ts is None or action.ts - b.ts <= window)]
    found = [j for j, b in enumerate(recent) if req in b.steps]
    if not found:
        return "missing", ("same_command_unsafe" if same else "not_run"), info
    j = found[-1]
    b = recent[j]
    r = _seg_index(b, [req])
    info["command"] = redact(action_plan(b)[r].core, 80) if action_plan(b) and r < len(action_plan(b)) else b.tool
    info["at"] = b.ts
    maybe = None
    if fresh:
        verdict = fingerprint(b, action, m, ignore) if fingerprint else None
        if verdict and verdict[0] == "same":
            info["fingerprint"] = "same"
        elif verdict and verdict[0] == "changed":
            info["changed"] = verdict[1]
            return "missing", "changed", info
        else:
            if verdict:
                info["fingerprint"] = verdict[1]
            between = recent[j + 1:]
            if others and b.ts is not None and action.ts is not None:
                between = between + [x for x in others(root, b.ts, action.ts, action.session_key)]
            later = [(x, action_writes(x, root)) for x in between]
            here = action_writes(action, root, upto=m) if segs else None
            hard = [x for x, w in later if w == "yes"]
            if hard or here == "yes":
                info["by"] = _describe(hard[-1]) if hard else "this command"
                culprit = hard[-1] if hard else action
                upto = None if hard else m
                switched = any(set(seg.steps) & {"git_update", "git_rewrite"} and writes(seg, root, culprit.command) == "yes"
                               for seg in action_plan(culprit)[:upto])
                return "missing", ("switched_version" if switched else "changed"), info
            soft = [x for x, w in later if w == "maybe"]
            if soft or here == "maybe":
                maybe = _describe(soft[-1]) if soft else "this command"
    if need_pass:
        res = step_result(b, r)
        if res == "fail":
            return "missing", "failed", info
        if res is None:
            segs_b = action_plan(b)
            info["hidden_by"] = "pipe" if segs_b and r < len(segs_b) and segs_b[r].after == "|" else "unrecorded"
            return "cant_tell", "result_hidden", info
    if maybe:
        info["by"] = maybe
        return "cant_tell", "maybe_changed", info
    return "ok", "ok", info


def _describe(action):
    if action.command:
        return "`%s`" % redact(action.command, 70)
    if action.target:
        return "%s %s" % (action.tool, redact(action.target, 60))
    return action.tool or "a tool call"


def check_before(rule, prior, action, fingerprint=None):
    """(status, detail, infos) for a require_before rule: status 'ok' | 'missing' | 'cant_tell'."""
    window = float(rule.get("within_minutes", 180)) * 60
    m = _seg_index(action, (rule.get("match") or {}).get("steps") or [])
    results = [evaluate_requirement(req, prior, action, m, need_pass=rule.get("passed", True),
                                    fresh=rule.get("fresh", False), window=window, fingerprint=fingerprint,
                                    ignore=rule.get("ignore") or ())
               for req in rule.get("requires") or []]
    missing = [r for r in results if r[0] == "missing"]
    unknown = [r for r in results if r[0] == "cant_tell"]
    chosen = missing or unknown
    if not chosen:
        return "ok", "", [r[2] for r in results]
    status = "missing" if missing else "cant_tell"
    return status, "; ".join(_short_reason(code, info) for _, code, info in chosen), [dict(r[2], code=r[1]) for r in chosen]


def _short_reason(code, info):
    n = noun(info["req"])
    return {
        "not_run": "%s did not run" % n,
        "same_command_unsafe": "%s in the same command would not stop it if they failed" % n,
        "failed": "%s failed the last time" % n,
        "result_hidden": "%s ran but the result was not recorded" % n,
        "changed": "files changed after %s last ran" % n,
        "switched_version": "another version was checked out or pulled after %s last ran" % n,
        "changed_in_command": "the command changes files after %s" % n,
        "maybe_changed": "a later command may have changed files after %s" % n,
        "maybe_changed_in_command": "the command may change files after %s" % n,
    }.get(code, code)


def fix_message(rule, action, infos, prior=()):
    """What the agent should do before retrying, in one or two sentences it can act on."""
    kind = next((s for s in action.steps if s in ((rule.get("match") or {}).get("steps") or [])), None)
    step = gerund(kind) if kind else "going ahead"
    parts = []
    for info in infos:
        req, code = info["req"], info.get("code")
        n = noun(req)
        last = info.get("command") or _last_command_for(prior, req)
        run_again = "Run %s again%s" % (n, " (`%s`)" % last if last else "")
        if code == "not_run":
            parts.append("%s did not run in this session. Run %s%s first." % (
                n[:1].upper() + n[1:], n, " (for example `%s`)" % last if last else ""))
        elif code == "same_command_unsafe":
            parts.append("The %s in this command would not stop it from %s if they failed (joined with `%s`). Join "
                         "them with `&&` (and put `set -o pipefail;` first when piping), or run them first." % (
                             n, step, info.get("join") or ";"))
        elif code == "failed":
            parts.append("%s failed the last time they ran. Fix the failure, then %s." % (n[:1].upper() + n[1:], lc(run_again)))
        elif code == "result_hidden":
            how = ("its output was piped (into `tail` or similar), which hides a failure" if info.get("hidden_by") == "pipe"
                   else "the log did not record its result")
            parts.append("The last %s run's result is unknown: %s. %s without the pipe, or put `set -o pipefail;` "
                         "first." % (n, how, run_again))
        elif code == "changed":
            changed = info.get("changed") or []
            if changed:
                shown = ", ".join(changed[:5]) + (" and %d more" % (len(changed) - 5) if len(changed) > 5 else "")
                what = "These files changed after %s last ran: %s." % (n, shown)
                if any(re.search(r"(?:^|/)(?:dist|build|out|coverage|generated|gen)/|\.(?:lock|min\.js|map)$", p) for p in changed):
                    what += " If some are generated, add them to the rule's \"ignore\" list."
            else:
                what = "Files changed after %s last ran (by %s)." % (n, info.get("by") or "a later call")
            parts.append("%s %s on the final version." % (what, run_again))
        elif code == "switched_version":
            parts.append("A different version was checked out or pulled after %s last ran (%s), so they did not run on "
                         "what is about to ship. %s on this version." % (n, info.get("by") or "a git command", run_again))
        elif code in ("changed_in_command", "maybe_changed_in_command"):
            parts.append("This command changes files after the %s. Run %s after the last change." % (n, n))
        elif code == "maybe_changed":
            parts.append("A later call may have changed files (%s). %s." % (info.get("by") or "a command", run_again))
    return " ".join(parts) + " Then retry."


# ---------------------------------------------------------------------------------------------------------------
# require_after, rehearsal and the live decision


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
                            "already_refused": 0, "would_hold": 0, "would_hold_each_call": 0, "would_fix": 0,
                            "would_block": 0, "satisfied": 0, "violations": 0, "cant_tell": 0, "examples": [],
                            "reasons": Counter(), "enforced_live": r.get("type") in LIVE}
    for key, acts in sessions_of(actions).items():
        counts = Counter()
        lo = defaultdict(int)
        window_start = {}
        for i, a in enumerate(acts):
            for r in rules:
                if not matches(r, a):
                    continue
                rid = r.get("id")
                if r.get("type") == "require_before" and a.ts is not None:
                    edge = a.ts - float(r.get("within_minutes", 180)) * 60
                    while lo[rid] < i and acts[lo[rid]].ts is not None and acts[lo[rid]].ts < edge:
                        lo[rid] += 1
                out = res[rid]
                out["matched"] += 1
                if not a.ran():
                    out["already_refused"] += 1
                t = r.get("type")
                why = None
                if t == "hold":
                    out["would_hold_each_call"] += 1
                    span = int(r.get("approval_minutes", 0) or 0) * 60
                    start = window_start.get(rid)
                    if span and start is not None and a.ts is not None and a.ts - start <= span:
                        out["satisfied"] += 1
                    else:
                        out["would_hold"] += 1
                        window_start[rid] = a.ts
                        why = "would wait for a person"
                elif t == "block":
                    out["would_block"] += 1
                    why = "would be refused"
                elif t == "require_before":
                    status, detail, infos = check_before(r, acts[lo[rid]:i], a)
                    for info in infos if status != "ok" else []:
                        out["reasons"][info.get("code")] += 1
                    if status == "ok":
                        out["satisfied"] += 1
                    else:
                        if status == "cant_tell":
                            out["cant_tell"] += 1
                        else:
                            out["violations"] += 1
                        mode = r.get("if_missing", "fix")
                        out["would_fix" if mode == "fix" else "would_block" if mode == "block" else "would_hold"] += 1
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
                        counts[(rid, k)] += 1
                    if counts[(rid, k)] > int(r.get("max", 0)):
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
    weeks = (days / 7.0) if days else None
    for out in res.values():
        out["reasons"] = dict(out["reasons"])
        if weeks:
            out["holds_per_week"] = round(out["would_hold"] / weeks, 1)
            out["holds_per_week_each_call"] = round(out["would_hold_each_call"] / weeks, 1)
            out["fixes_per_week"] = round(out["would_fix"] / weeks, 1)
    unsupported = [{"id": r.get("id"), "says": r.get("says"), "note": r.get("note")}
                   for r in doc.get("rules") or [] if r.get("type") == "unsupported"]
    result = {"name": doc.get("name"), "rules": list(res.values()), "unsupported": unsupported}
    if weeks:
        result["budget"] = interruption_budget(doc, result["rules"])
    return result


def interruption_budget(doc, rows):
    """How often the rules would stop to ask a person, against the budget the person set (default 10 a week)."""
    limit = ((doc.get("budget") or {}).get("asks_per_week")) or DEFAULT_BUDGET
    asks = round(sum(r.get("holds_per_week", 0) for r in rows), 1)
    advice = []
    by_id = {r.get("id"): r for r in doc.get("rules") or []}
    for row in sorted(rows, key=lambda x: -x.get("holds_per_week", 0)):
        rule = by_id.get(row["id"]) or {}
        if row.get("holds_per_week", 0) < 1:
            continue
        if rule.get("type") == "hold" and not rule.get("approval_minutes"):
            advice.append("%s: let one approval cover the rest of the task (\"approval_minutes\": 60)." % row["id"])
        if rule.get("type") == "hold" and not (rule.get("match") or {}).get("command"):
            advice.append("%s: hold only the ones that matter, for example production (\"command\": \"--prod|production|main\")."
                          % row["id"])
        if rule.get("type") == "require_before" and rule.get("if_missing") == "hold":
            advice.append("%s: tell the agent what to run instead of asking you (\"if_missing\": \"fix\")." % row["id"])
    return {"asks_per_week": asks, "limit": limit, "over": asks > limit, "advice": advice[:4],
            "fixes_per_week": round(sum(r.get("fixes_per_week", 0) for r in rows), 1)}


def active_grant(rule, action, grants):
    """A standing approval the person gave from their own terminal (`replay.py approve`), still in force."""
    for g in reversed(grants or []):
        if g.get("rule") not in (rule.get("id"), "*"):
            continue
        if g.get("session") and g.get("session") != action.session:
            continue
        if action.ts is not None and g.get("until", 0) >= action.ts and g.get("at", 0) <= action.ts:
            return g
    return None


def covered_by_approval(rule, action, prior, asked, grants=None):
    """An approval that covers this call: a standing approval from the person's terminal, or (with
    `approval_minutes`) an earlier call this rule asked about in the same session that then ran, so they said yes.
    Returns a record describing it, or None."""
    g = active_grant(rule, action, grants)
    if g:
        return {"tool_use_id": "grant:%s" % g.get("id"), "grant": g}
    span = int(rule.get("approval_minutes", 0) or 0) * 60
    if not span or not asked:
        return None
    ran = {b.id for b in prior if b.ran() and b.outcome != "unknown"}
    for rec in reversed(asked):
        if rec.get("rule") != rule.get("id") or rec.get("tool_use_id") not in ran:
            continue
        if action.ts is not None and rec.get("ts") is not None and action.ts - rec["ts"] <= span:
            return rec
    return None


def decide(doc, action, prior, day_counts=None, fingerprint=None, asked=None, grants=None):
    """Live decision for one tool call: (decision, rule, reason, info) with decision 'deny', 'ask' or None.

    'deny' reasons are written for the agent (Claude reads them); 'ask' reasons for the person approving."""
    day_counts = day_counts or {}
    best = (None, None, None, {})
    rank = {"deny": 2, "ask": 1, None: 0}
    for r in doc.get("rules") or []:
        t = r.get("type")
        if t not in LIVE or not matches(r, action):
            continue
        decision, reason, info = None, None, {}
        if t == "block":
            decision = "deny"
            reason = "%s This is refused by the rule; do not retry it. Tell the person if it is needed." % r.get("says")
        elif t == "hold":
            rec = covered_by_approval(r, action, prior, asked, grants)
            if rec:
                info = {"covered_by": rec.get("tool_use_id")}
            else:
                decision = "ask"
                span = int(r.get("approval_minutes", 0) or 0)
                steps = (r.get("match") or {}).get("steps") or []
                reason = r.get("says") or "A person approves this."
                if span:
                    reason += " Approving also covers %s in this session for the next %d minutes." % (
                        _join([gerund(s) for s in steps]) if steps else "the rule's other matching calls", span)
        elif t == "require_before":
            status, detail, infos = check_before(r, prior, action, fingerprint)
            if status != "ok":
                mode = r.get("if_missing", "fix")
                info = {"missing": infos}
                if mode == "hold":
                    decision, reason = "ask", "%s (%s)" % (r.get("says"), detail)
                else:
                    decision = "deny"
                    reason = "%s %s" % (r.get("says"), fix_message(r, action, infos, prior) if mode == "fix"
                                        else "Refused (%s). Tell the person." % detail)
        elif t == "limit":
            used = day_counts.get(r.get("id"), 0)
            if r.get("per") == "session":
                used = sum(1 for b in prior if b.ran() and matches(r, b))
            if used >= int(r.get("max", 0)):
                decision = "deny"
                reason = "%s Already %d; refused. Tell the person if more are needed." % (r.get("says"), used)
        if rank[decision] > rank[best[0]] or (best[1] is None and info):
            best = (decision, r, reason, info)
    return best


def _join(items):
    items = [i for i in items if i]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


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
            span = int(r.get("approval_minutes", 0) or 0)
            how = "Waits for a person %s%s." % (when, "; one approval covers the rest of the session's matching calls for %d minutes" % span if span else "")
        elif t == "block":
            how = "Refused " + when + "."
        elif t == "require_before":
            mode = r.get("if_missing", "fix")
            then = {"fix": "the agent is told what to run first", "hold": "a person decides",
                    "block": "it is refused"}[mode if mode in IF_MISSING else "fix"]
            how = "%s, unless %s ran first%s%s; otherwise %s." % (
                when[:1].upper() + when[1:], " and ".join(noun(x) for x in r.get("requires") or []),
                " and passed" if r.get("passed", True) else "",
                " on the final version (compared by file content when live)" if r.get("fresh") else "", then)
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
