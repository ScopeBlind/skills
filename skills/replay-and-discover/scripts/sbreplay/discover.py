"""Find how the work actually flows.

Everything here is computed from the actions `sources` produced. The history is the agent's own log, so every
finding is labelled as self-reported evidence: it shows what the agent recorded, not what an independent party saw.
"""
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .classify import (CHECKS, FINISH, FLOW_STEPS, RISKY, UNPLACED, focus, gerund, label, meaningful_segments, noun, shape,
                       split_commands, strip_heredocs)
from .redact import find_secrets, redact, short_path

DONE_WINDOW = 3 * 3600          # look back this far (seconds) for the checks before a finishing step
AFTER_DEPLOY = 30 * 60          # a live check must follow a deploy or publish within this many seconds
AFTER_PUSH = 60 * 60            # a CI check must follow a push or pull request within this many seconds
IMPLICIT_SHARE = 0.6            # a check that precedes at least this share of a step is part of its usual "done"
MIN_OCCURRENCES = 5
LOOP_STEPS = ("test", "typecheck", "build", "lint")
INSPECT = re.compile(r"^(?:cd|ls|cat|head|tail|less|grep|rg|sed|awk|echo|printf|wc|find|pwd|which|stat|file|jq|"
                     r"sort|uniq|cut|tr|date|sleep|true|false|test|\[)\b")


def _day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def _project_name(path):
    if not path:
        return "(unknown)"
    return os.path.basename(path.rstrip("/")) or short_path(path)


def example(action, focus_steps=None, secret=False, limit=140):
    """A redacted, one-line description of an action for reports, showing the part of the command that mattered."""
    body = action.command or action.target or action.tool or ""
    if action.command:
        part = None
        if secret:
            part = next((seg for seg in split_commands(strip_heredocs(action.command)) if find_secrets(seg)), None)
        elif focus_steps:
            part = focus(action.command, focus_steps)
        if part:
            more = len(split_commands(strip_heredocs(action.command))) > 1
            body = part + (" …" if more else "")
    return {
        "when": _day(action.ts),
        "project": _project_name(action.project),
        "session": (action.session or "")[:8],
        "agent": action.agent,
        "what": redact(body, limit),
        "outcome": action.outcome,
    }


def sessions_of(actions):
    groups = defaultdict(list)
    for a in actions:
        groups[a.session_key].append(a)
    return groups


def _events(session_actions):
    """Flatten a session's actions into (action, step) pairs in the order the steps ran."""
    out = []
    for a in session_actions:
        for st in a.steps:
            out.append((a, st))
    return out


def overview(actions, since, until):
    agents = Counter()
    sessions = defaultdict(set)
    subagent_sessions = set()
    kinds = Counter()
    outcomes = Counter()
    projects = Counter()
    days = set()
    for a in actions:
        agents[a.agent] += 1
        sessions[a.agent].add(a.session)
        if a.subagent:
            subagent_sessions.add(a.session_key)
        outcomes[a.outcome] += 1
        projects[short_path(a.project)] += 1
        if a.ts:
            days.add(_day(a.ts))
        if a.tool in ("Bash", "shell"):
            kinds["commands"] += 1
        elif "edit" in a.steps:
            kinds["edits"] += 1
        elif "read" in a.steps:
            kinds["reads"] += 1
        elif "web" in a.steps:
            kinds["web"] += 1
        elif "mcp" in a.steps:
            kinds["connected tools"] += 1
        elif "delegate" in a.steps:
            kinds["sub-agents"] += 1
        else:
            kinds["other"] += 1
    first = min((a.ts for a in actions if a.ts), default=None)
    last = max((a.ts for a in actions if a.ts), default=None)
    return {
        "window": {"since": _day(since) if since else None, "until": _day(until) if until else None,
                   "first_action": _day(first), "last_action": _day(last), "active_days": len(days)},
        "actions": len(actions),
        "by_agent": {k: {"actions": v, "sessions": len(sessions[k])} for k, v in agents.items()},
        "subagent_sessions": len(subagent_sessions),
        "by_kind": dict(kinds.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "top_projects": [{"project": p, "actions": n} for p, n in projects.most_common(8)],
    }


def _passed(action, kind):
    """Whether the `kind` step in an action succeeded, reading through pipes and joins (see rules.step_result)."""
    from .rules import _seg_index, step_result
    key = ("passed", kind)
    if key not in action.cache:
        action.cache[key] = step_result(action, _seg_index(action, [kind])) == "pass"
    return action.cache[key]


def other_sessions(actions):
    """A lookup of actions that may change files, by time, so a release can see edits made by other sessions."""
    import bisect
    from .rules import action_plan
    from .classify import _WRITE_KINDS, _WRITER_HEAD
    writers = []
    for a in actions:
        if a.ts is None or not a.ran():
            continue
        if "edit" in a.steps or any((set(seg.steps) & _WRITE_KINDS) or ">" in (seg.core or "") or _WRITER_HEAD.match(seg.core or "")
                                    for seg in action_plan(a)):
            writers.append(a)
    writers.sort(key=lambda a: a.ts)
    times = [a.ts for a in writers]

    from .classify import real_path

    def lookup(root, since, until, session_key):
        lo, hi = bisect.bisect_right(times, since), bisect.bisect_left(times, until)
        out = []
        for x in writers[lo:hi]:
            where = real_path(x.project) if x.project else None
            if x.session_key != session_key and (not root or not where or where.startswith(root) or root.startswith(where)):
                out.append(x)
        return out
    return lookup


def definition_of_done(groups, max_examples=3, others=None):
    """For each finishing step, which checks usually come first, how often the final version was tested, and the
    exceptions to the usual pattern.

    "Final version tested" is a range. The low end counts only releases where a passing test provably saw the final
    files: no edit or file-changing command in between, and a result the log recorded (a test piped into `tail`
    counts only if the runner's own summary says it passed). The high end adds the releases the log cannot settle
    either way."""
    from .rules import _seg_index, evaluate_requirement
    stats = {}
    for f in FINISH:
        stats[f] = {"n": 0, "present": Counter(), "passed": Counter(), "tested_final": 0, "unclear": 0,
                    "changed_after": 0, "reasons": Counter(), "occurrences": []}
    for key, acts in groups.items():
        ev = _events(acts)
        position = {id(a): i for i, a in enumerate(acts)}
        last_seen = {}
        for i, (a, st) in enumerate(ev):
            if st not in FINISH or not a.ran():
                continue
            start = last_seen.get(st, -1) + 1
            last_seen[st] = i
            window = [(b, s) for (b, s) in ev[start:i] if a.ts is None or b.ts is None or a.ts - b.ts <= DONE_WINDOW]
            present = {s for (b, s) in window if s in CHECKS}
            passed = {s for (b, s) in window if s in CHECKS and _passed(b, s)}
            status, code, _ = evaluate_requirement("test", acts[:position[id(a)]], a, _seg_index(a, [st]),
                                                   need_pass=True, fresh=True, window=DONE_WINDOW, others=others)
            rec = stats[st]
            rec["n"] += 1
            for c in present:
                rec["present"][c] += 1
            for c in passed:
                rec["passed"][c] += 1
            rec["tested_final"] += 1 if status == "ok" else 0
            rec["unclear"] += 1 if status == "cant_tell" else 0
            rec["changed_after"] += 1 if code in ("changed", "changed_in_command", "switched_version") else 0
            rec["reasons"][code] += 1
            rec["occurrences"].append((a, present, status == "ok"))
    out = {}
    for f, rec in stats.items():
        n = rec["n"]
        if not n:
            continue
        implicit = [c for c in CHECKS if n >= MIN_OCCURRENCES and rec["present"][c] / float(n) >= IMPLICIT_SHARE]
        exceptions = [a for (a, present, _) in rec["occurrences"] if implicit and not set(implicit) <= present]
        out[f] = {
            "label": label(f),
            "n": n,
            "checks_before": {c: {"present": rec["present"][c], "passed": rec["passed"][c],
                                  "share": round(rec["present"][c] / float(n), 3)} for c in CHECKS if rec["present"][c]},
            "tested_final_version": rec["tested_final"],
            "tested_final_version_max": rec["tested_final"] + rec["unclear"],
            "changed_after_test": rec["changed_after"],
            "why_not": dict(rec["reasons"]),
            "implicit_done": implicit,
            "exceptions": len(exceptions),
            "exception_examples": [example(a, [f]) for a in exceptions[:max_examples]],
        }
    return out


def after_checks(groups, max_examples=3):
    """Does anything check the result after a deploy, publish, push or pull request?"""
    res = {}
    rules = [("deploy", ("web_request", "test"), AFTER_DEPLOY), ("publish", ("web_request", "test"), AFTER_DEPLOY),
             ("push", ("ci_check",), AFTER_PUSH), ("pr_open", ("ci_check",), AFTER_PUSH)]
    for step, follow, window in rules:
        n, followed, missing = 0, 0, []
        for key, acts in groups.items():
            ev = _events(acts)
            for i, (a, st) in enumerate(ev):
                if st != step or a.outcome not in ("ok", "unknown"):
                    continue
                n += 1
                ok = False
                for j in range(i + 1, len(ev)):
                    b, s = ev[j]
                    if a.ts is not None and b.ts is not None and b.ts - a.ts > window:
                        break
                    if s in follow and b.ran():
                        ok = True
                        break
                if ok:
                    followed += 1
                else:
                    missing.append(a)
        if n:
            res[step] = {"label": label(step), "n": n, "checked_after": followed,
                         "checked_with": [label(s) for s in follow], "within_minutes": window // 60,
                         "missing_examples": [example(a, [step]) for a in missing[:max_examples]]}
    return res


def evidence(done, after):
    """What the history can already show for each finishing step. All of it is self-reported by the agent."""
    out = {}
    for step in ("push", "pr_open", "pr_merge", "deploy", "publish"):
        d = done.get(step)
        if not d:
            continue
        cb = d["checks_before"]
        out[step] = {
            "label": d["label"], "n": d["n"],
            "passing_test_before": cb.get("test", {}).get("passed", 0),
            "tested_final_version": d["tested_final_version"],
            "tested_final_version_max": d["tested_final_version_max"],
            "ci_checked_before": cb.get("ci_check", {}).get("present", 0),
            "checked_after": (after.get(step) or {}).get("checked_after"),
            "strength": "self-reported (the agent's own log)",
        }
    return out


def risky(actions, max_examples=3):
    res = {}
    for kind in RISKY:
        acts = [a for a in actions if kind in a.steps]
        if not acts:
            continue
        oc = Counter(a.outcome for a in acts)
        res[kind] = {
            "label": label(kind), "attempts": len(acts), "ran": oc.get("ok", 0), "failed": oc.get("failed", 0),
            "refused": oc.get("denied", 0), "unknown": oc.get("unknown", 0) + oc.get("error", 0) + oc.get("interrupted", 0),
            "sessions": len({a.session_key for a in acts}),
            "projects": [p for p, _ in Counter(_project_name(a.project) for a in acts).most_common(3)],
            "examples": [example(a, [kind]) for a in acts[-max_examples:]],
        }
    return res


def friction(actions, max_examples=5):
    denied = [a for a in actions if a.outcome == "denied"]
    by_step = Counter((a.steps[0] if a.steps else "other_command") for a in denied)
    sig = Counter((_signature(a.command) or _first_words(a.command)) if a.command else (a.tool or "?") for a in denied)
    return {"refused": len(denied), "by_step": {label(k): v for k, v in by_step.most_common(8)},
            "top_refused": [{"pattern": redact(k, 80), "count": v} for k, v in sig.most_common(max_examples)],
            "examples": [example(a, a.steps) for a in denied[-max_examples:]]}


def _result(action, kind):
    from .rules import _seg_index, step_result
    return step_result(action, _seg_index(action, [kind]))


def rework(groups):
    runs = failed = loops = 0
    attempts = []
    by_project = Counter()
    for key, acts in groups.items():
        for kind in LOOP_STEPS:
            seq = [a for a in acts if kind in a.steps and a.ran()]
            runs += len(seq)
            streak = 0
            for a in seq:
                result = _result(a, kind)
                if result == "fail":
                    failed += 1
                    streak += 1
                elif result == "pass" and streak:
                    loops += 1
                    attempts.append(streak + 1)
                    by_project[_project_name(a.project)] += 1
                    streak = 0
    return {"check_runs": runs, "failed_runs": failed, "fix_and_rerun_loops": loops,
            "median_attempts_to_pass": statistics.median(attempts) if attempts else None,
            "top_projects": [p for p, _ in by_project.most_common(3)]}


def _flow_tokens(acts):
    tokens = []
    for a in acts:
        if not a.ran():
            continue
        for st in a.steps:
            if st not in FLOW_STEPS:
                continue
            if tokens and tokens[-1][0] == st:
                tokens[-1][2] = a
            else:
                tokens.append([st, a, a])
    return tokens


def workflows(groups, top=6, lengths=(3, 4, 5)):
    """Recurring step sequences that end in shipping something (commit, push, merge, deploy, publish)."""
    counts, sess, spans, projects = Counter(), defaultdict(set), defaultdict(list), defaultdict(Counter)
    for key, acts in groups.items():
        toks = _flow_tokens(acts)
        for n in lengths:
            for i in range(len(toks) - n + 1):
                gram = tuple(t[0] for t in toks[i:i + n])
                if not any(g in FINISH or g in RISKY for g in gram):
                    continue
                counts[gram] += 1
                sess[gram].add(key)
                a0, a1 = toks[i][1], toks[i + n - 1][2]
                if a0.ts and a1.ts:
                    spans[gram].append(a1.ts - a0.ts)
                projects[gram][_project_name(a0.project)] += 1
    ranked = sorted((g for g in counts if counts[g] >= 3 and len(sess[g]) >= 2),
                    key=lambda g: (-(counts[g] * len(g) * (2 if g[-1] in FINISH else 1)), g))
    chosen = []
    for g in ranked:
        if any(_contains(c, g) and counts[g] <= 1.25 * counts[c] for c in chosen):
            continue
        chosen.append(g)
        if len(chosen) >= top:
            break
    return [{"steps": [label(s) for s in g], "kinds": list(g), "times": counts[g], "sessions": len(sess[g]),
             "median_minutes": round(statistics.median(spans[g]) / 60.0, 1) if spans[g] else None,
             "projects": [p for p, _ in projects[g].most_common(3)]} for g in chosen]


def _contains(big, small):
    n = len(small)
    return any(tuple(big[i:i + n]) == tuple(small) for i in range(len(big) - n + 1))


def _signature(command):
    """A short, generalised form of a command for spotting repeats: the first meaningful command, first three words."""
    segs = [x for x in meaningful_segments(command) if not INSPECT.match(x)]
    if not segs:
        return ""
    words = segs[0].split()[:3]
    out = []
    for w in words:
        if "/" in w or w.startswith("~"):
            out.append("<path>")
        elif re.match(r"^['\"]", w):
            out.append("<text>")
        elif re.search(r"[0-9a-f]{7,}", w) or re.match(r"^-?\d+$", w):
            out.append("<n>")
        else:
            out.append(w)
    return " ".join(out)


def _first_words(command, n=2):
    segs = split_commands(strip_heredocs(command or ""))
    return " ".join(segs[0].split()[:n]) if segs else "?"


def automation(groups, top=5):
    """Sequences of commands repeated often enough to be worth a script or a skill."""
    counts, sess = Counter(), defaultdict(set)
    for key, acts in groups.items():
        sigs = []
        for a in acts:
            if a.command and a.ran():
                s = _signature(a.command)
                if s and (not sigs or sigs[-1] != s):
                    sigs.append(s)
        for n in (3, 4):
            for i in range(len(sigs) - n + 1):
                g = tuple(sigs[i:i + n])
                if len(set(g)) < n:
                    continue
                counts[g] += 1
                sess[g].add(key)
    ranked = sorted((g for g in counts if counts[g] >= 5 and len(sess[g]) >= 2), key=lambda g: (-counts[g] * len(g), g))
    chosen = []
    for g in ranked:
        if any(_contains(c, g) and counts[g] <= 1.25 * counts[c] for c in chosen):
            continue
        chosen.append(g)
        if len(chosen) >= top:
            break
    return [{"sequence": [redact(s, 60) for s in g], "times": counts[g], "sessions": len(sess[g])} for g in chosen]


def secrets(actions, max_examples=3):
    hits = [(a, find_secrets(a.command)) for a in actions if a.command]
    hits = [(a, labels) for a, labels in hits if labels]
    kinds = Counter(l for _, labels in hits for l in labels)
    return {"commands": len(hits), "sessions": len({a.session_key for a, _ in hits}), "kinds": dict(kinds),
            "examples": [example(a, secret=True) for a, _ in hits[:max_examples]]}


def suggestions(done, after, risk, secret, fric, loops, auto):
    """Deterministic suggestions, each with the evidence behind it and a draft rule the person can accept."""
    out = []
    for step in ("push", "pr_open", "deploy", "publish", "commit"):
        d = done.get(step)
        if d and d["implicit_done"] and d["exceptions"]:
            checks = d["implicit_done"]
            names = " and ".join(noun(c) for c in checks)
            out.append({
                "title": "Make your usual checks before %s a rule" % gerund(step),
                "evidence": "%s ran before %d of %d; %d went ahead without." % (
                    names[:1].upper() + names[1:], d["n"] - d["exceptions"], d["n"], d["exceptions"]),
                "rule": {"id": "%s-before-%s" % ("-".join(checks), step), "type": "require_before",
                         "says": "%s pass%s on the final version before %s." % (names[:1].upper() + names[1:], "" if checks == ["test"] else "es", gerund(step)),
                         "match": {"steps": [step]}, "requires": checks, "passed": True, "fresh": True,
                         "if_missing": "fix"}})
    push = done.get("push")
    if push and push["n"] >= 5 and push["tested_final_version"] < 0.8 * push["n"] and not any(
            "push" in (o.get("rule") or {}).get("match", {}).get("steps", []) for o in out):
        out.insert(0, {
            "title": "Test the final version before pushing",
            "evidence": "%d of %d pushes shipped a version a passing test had provably run on." % (
                push["tested_final_version"], push["n"]),
            "rule": {"id": "tests-before-push", "type": "require_before",
                     "says": "Tests pass on the final version before anything is pushed or a pull request is opened.",
                     "match": {"steps": ["push", "pr_open"]}, "requires": ["test"], "passed": True, "fresh": True,
                     "if_missing": "fix"}})
    for step in ("deploy", "publish"):
        a = after.get(step)
        if a and a["checked_after"] < a["n"]:
            out.append({
                "title": "Check the live result after %s" % gerund(step),
                "evidence": "%d of %d had no check within %d minutes." % (a["n"] - a["checked_after"], a["n"], a["within_minutes"]),
                "rule": {"id": "check-after-%s" % step, "type": "require_after",
                         "says": "The live result is checked within %d minutes of %s." % (a["within_minutes"], gerund(step)),
                         "match": {"steps": [step]}, "requires": ["web_request", "test"], "within_minutes": a["within_minutes"]}})
    held = [k for k in ("publish", "deploy", "pr_merge", "db_change", "cloud_change") if k in risk and risk[k]["ran"]]
    if held:
        out.append({
            "title": "Have a person approve " + _join([gerund(k) for k in held]),
            "evidence": "; ".join("%s: %s" % (label(k), _count(risk[k]["ran"], "time")) for k in held) + ".",
            "rule": {"id": "approve-consequential", "type": "hold",
                     "says": "A person approves before %s." % _join([gerund(k) for k in held]),
                     "match": {"steps": held}, "approval_minutes": 60}})
    if "force_push" in risk:
        out.append({"title": "Never force-push to main",
                    "evidence": "%s in %s." % (_count(risk["force_push"]["attempts"], "force-push").replace("force-pushs", "force-pushes"), _count(risk["force_push"]["sessions"], "session")),
                    "rule": {"id": "no-force-push-main", "type": "block", "says": "The agent never force-pushes to main or master.",
                             "match": {"steps": ["force_push"], "command": r"\b(main|master)\b"}}})
    if secret["commands"]:
        out.append({"title": "Keep secrets out of commands",
                    "evidence": "%s carried a literal secret (%s)." % (_count(secret["commands"], "command"), ", ".join(sorted(secret["kinds"]))),
                    "rule": {"id": "no-literal-secrets", "type": "block", "says": "No command contains a literal secret.",
                             "match": {"secrets": True}}})
    if fric["refused"] >= 20 and fric["top_refused"]:
        top = fric["top_refused"][0]
        out.append({"title": "Look at what keeps getting refused",
                    "evidence": "%d calls were refused; most often `%s` (%d)." % (fric["refused"], top["pattern"], top["count"]),
                    "rule": None})
    if loops["fix_and_rerun_loops"] >= 10:
        out.append({"title": "Shorten the fix-and-rerun loop",
                    "evidence": "%d loops; a check usually passed on attempt %s." % (loops["fix_and_rerun_loops"], loops["median_attempts_to_pass"]),
                    "rule": None})
    for seq in auto[:2]:
        out.append({"title": "Turn a repeated sequence into a script or skill",
                    "evidence": "%s: %d times in %d sessions." % (" -> ".join(seq["sequence"]), seq["times"], seq["sessions"]),
                    "rule": None})
    return out


def _count(n, word):
    return "%d %s" % (n, word if n == 1 else word + "s")


def _join(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


_CONSEQUENTIAL = re.compile(r"(?i)(?:^|[^a-z])(?:ship|release|deploy|publish|prod|production|promote|upload|sync|"
                           r"migrate|migration|rollout|launch|push|tag|bump|gate|merge|backup|restore|provision|seed|stage)"
                           r"(?:[^a-z]|$)")


def unplaced(actions, top=15):
    """Commands the patterns could not name that keep coming back: a project's own scripts are often its release
    steps (`./scripts/ship.sh`, `npm run release`). The agent running this skill can label them with the person."""
    from .rules import action_plan
    counts, sess, projects, sample = Counter(), defaultdict(set), defaultdict(Counter), {}
    for a in actions:
        if not a.command or not a.ran():
            continue
        for seg in action_plan(a):
            if not seg.steps or seg.steps[-1] not in UNPLACED:
                continue
            key = shape(seg.raw)
            if not key or len(key) < 3:
                continue
            counts[key] += 1
            sess[key].add(a.session_key)
            projects[key][_project_name(a.project)] += 1
            sample.setdefault(key, (a, seg.raw))
    rows = []
    for key, n in counts.most_common():
        if n < 3 or len(sess[key]) < 2:
            continue
        a, raw = sample[key]
        rows.append({"shape": redact(key, 80), "times": n, "sessions": len(sess[key]),
                     "projects": [p for p, _ in projects[key].most_common(3)],
                     "sounds_consequential": bool(_CONSEQUENTIAL.search(key)),
                     "example": redact(raw, 140)})
    rows.sort(key=lambda r: (not r["sounds_consequential"], -r["times"]))
    return rows[:top]


def headline(findings):
    """The one sentence, three findings and one decision to lead with. Deterministic: chosen from the numbers."""
    done, after, secret = findings["definition_of_done"], findings["after_checks"], findings["secrets"]
    items = []
    lead = None
    for step in ("push", "pr_merge", "deploy", "pr_open", "publish"):
        d = done.get(step)
        if not d or d["n"] < 5:
            continue
        lo, hi = d["tested_final_version"], d.get("tested_final_version_max", d["tested_final_version"])
        why = {k: v for k, v in (d.get("why_not") or {}).items() if k not in ("ok", "same_command")}
        top = max(why.items(), key=lambda kv: kv[1]) if why else None
        span = ("%d%%" % round(100.0 * lo / d["n"])) if lo == hi else "%d%% to %d%%" % (
            round(100.0 * lo / d["n"]), round(100.0 * hi / d["n"]))
        text = "%s of your %d %s shipped a version that a passing test had provably run on" % (
            span[:1].upper() + span[1:], d["n"], _PLURAL.get(step, label(step).lower() + "s"))
        if top:
            text += "; the most common gap: %s (%d)" % (REASON_WORDS.get(top[0], top[0]), top[1])
        entry = {"step": step, "text": text + ".", "low": lo, "high": hi, "n": d["n"]}
        if lead is None or (step == "push" and lead["step"] != "push"):
            lead = entry
        items.append(entry)
    if secret.get("commands"):
        items.insert(0, {"step": "secrets", "text": "%s carried a literal secret (%s). Rotate those secrets: they are "
                         "stored in the agents' history." % (_count(secret["commands"], "command"), ", ".join(sorted(secret["kinds"])))})
    dep = after.get("deploy")
    if dep and dep["n"] >= 5 and dep["checked_after"] < dep["n"]:
        items.append({"step": "after_deploy", "text": "%d of %d deploys had no check of the live result within %d minutes." % (
            dep["n"] - dep["checked_after"], dep["n"], dep["within_minutes"])})
    decision = None
    for sg in findings.get("suggestions") or []:
        if sg.get("rule") and sg["rule"].get("type") in ("require_before", "hold", "block"):
            decision = {"question": "Turn this on: %s" % sg["rule"]["says"], "evidence": sg["evidence"], "rule": sg["rule"]}
            break
    sentence = lead["text"] if lead else "Not enough releases in this window to judge how finished work gets checked."
    rest = [i["text"] for i in items if not lead or i.get("step") != lead["step"]][:3]
    return {"sentence": sentence, "findings": rest, "decision": decision}


_PLURAL = {"push": "pushes", "pr_merge": "merged pull requests", "pr_open": "pull requests opened", "deploy": "deploys",
           "publish": "publishes", "commit": "commits"}
REASON_WORDS = {
    "not_run": "no test ran first",
    "failed": "the last test had failed",
    "result_hidden": "the test result was not recorded (its output was piped, for example into tail)",
    "changed": "files changed after the last passing test",
    "switched_version": "another version was checked out or pulled after the test",
    "maybe_changed": "a later command may have changed files",
    "same_command_unsafe": "a test in the same command would not have stopped it",
    "changed_in_command": "the same command changed files after the test",
    "maybe_changed_in_command": "the same command may have changed files after the test",
}


def discover(actions, since=None, until=None, max_examples=3):
    groups = sessions_of(actions)
    done = definition_of_done(groups, max_examples, other_sessions(actions))
    after = after_checks(groups, max_examples)
    risk = risky(actions, max_examples)
    secret = secrets(actions, max_examples)
    fric = friction(actions)
    loops = rework(groups)
    auto = automation(groups)
    out = {
        "overview": overview(actions, since, until),
        "evidence": evidence(done, after),
        "workflows": workflows(groups),
        "definition_of_done": done,
        "after_checks": after,
        "risky": risk,
        "secrets": secret,
        "friction": fric,
        "rework": loops,
        "automation": auto,
        "suggestions": suggestions(done, after, risk, secret, fric, loops, auto),
        "unplaced": unplaced(actions),
    }
    out["headline"] = headline(out)
    return out
