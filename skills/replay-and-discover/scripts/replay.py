#!/usr/bin/env python3
"""Replay your agents' local history, discover how the work flows, and turn what you want into rules they follow.

    replay.py scan       [--days 30] [--project TEXT] [--agents claude-code,codex] [--rules FILE] [--github]
                         [--compare FILE|last] [--full]
    replay.py rehearse   --rules FILE [same options as scan]
    replay.py init-rules [--out FILE] [--from FINDINGS_JSON] [--force]
    replay.py explain    --rules FILE
    replay.py label      --rules FILE --as STEP --pattern REGEX [--days 30] [--dry-run]
    replay.py install    [--agent claude-code|codex|both] [--scope user|project] [--rules FILE] [--dry-run] [--trust]
    replay.py uninstall  [--agent claude-code|codex|both] [--scope user|project] [--dry-run]
    replay.py status
    replay.py approve    RULE [--minutes 60] [--session ID] [--plan TEXT]   |   approve --list | --revoke ID | --clear
    replay.py enable     --rules FILE        (and: disable)
    replay.py connect    --report URL        (asks for the page's write token; --disconnect to forget it)
    replay.py share      --rules FILE [--days 30] [--out DIR]
    replay.py digest     [--days 7] [--quiet]
    replay.py weekly     on|off
    replay.py version

Reads ~/.claude/projects and ~/.codex/sessions on this machine. Writes reports to ~/.scopeblind/replay/ (private to
you). Sends nothing anywhere, with two exceptions you switch on yourself: --github reads merged pull requests with your
own `gh` login, and `connect` lets rules with "approver": "page" hold an action on your standard's page.
"""
import argparse
import difflib
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sbreplay import __version__  # noqa: E402
from sbreplay import discover as disc  # noqa: E402
from sbreplay import report, rules  # noqa: E402
from sbreplay.classify import STEPS, plan, shape  # noqa: E402
from sbreplay.redact import redact, short_path  # noqa: E402
from sbreplay.sources import load_actions  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "enforce_hook.py")
STARTER = os.path.join(os.path.dirname(HERE), "assets", "starter-rules.json")
HOME = os.environ.get("SCOPEBLIND_REPLAY_HOME") or os.path.expanduser("~/.scopeblind/replay")
STATE = os.path.join(HOME, "state")
DEFAULT_OUT = HOME
USER_RULES = os.path.expanduser("~/.scopeblind/rules.json")
CLAUDE_USER = os.path.expanduser("~/.claude/settings.json")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _say(text):
    sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def _err(text):
    sys.stderr.write(text + ("" if text.endswith("\n") else "\n"))


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_private(path, text):
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _git_root(path, common=False):
    args = ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"] if common else \
        ["git", "-C", path, "rev-parse", "--show-toplevel"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return os.path.dirname(value) if common else value


# ---------------------------------------------------------------------------------------------------------------
# scan, rehearse


def _window(args):
    now = time.time()
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    else:
        since = now - args.days * 86400
    return since, now, max(1, int(round((now - since) / 86400.0)))


def _load_rules(path):
    if not path:
        return None
    doc = rules.load(path)
    bad = rules.problems(doc)
    if bad:
        _err("The rules file has problems:\n" + "\n".join("  - " + b for b in bad))
        sys.exit(2)
    return doc


def _latest(kind="scan"):
    rec = _read_json(os.path.join(HOME, "latest-%s.json" % kind), None)
    return rec.get("findings") if rec and os.path.isfile(rec.get("findings") or "") else None


def cmd_scan(args, rehearse_only=False):
    doc = _load_rules(args.rules)
    since, until, days = _window(args)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    t0 = time.time()
    if not args.quiet:
        _err("Reading %s history for the last %d days..." % (" and ".join(agents), days))
    actions = load_actions(agents=agents, since=since, until=until, claude_dir=args.claude_dir,
                           codex_dir=args.codex_dir, extra=(doc or {}).get("_extra"), project=args.project)
    if not args.quiet:
        _err("Read {:,} actions in {:.0f}s. Finding patterns...".format(len(actions), time.time() - t0))
    findings = disc.discover(actions, since, until, max_examples=args.examples)
    findings["meta"] = {"tool": "replay-and-discover", "version": __version__, "days": days,
                        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "agents": agents, "project_filter": args.project, "seconds": round(time.time() - t0, 1)}
    rehearsal = rules.rehearse(doc, actions, days=days, max_examples=args.examples) if doc else None
    github = None
    if args.github:
        from sbreplay import github as gh
        projects = sorted({a.project for a in actions if a.project})
        since_date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")
        github = gh.merged_evidence(gh.repos_for(projects), since_date)
    changes = None
    compare = _latest() if args.compare == "last" else args.compare
    if compare:
        try:
            with open(compare, "r", encoding="utf-8") as fh:
                changes = report.compare(findings, json.load(fh))
        except (OSError, ValueError) as e:
            _err("Could not read %s for comparison: %s" % (compare, e))
    out_dir = args.out or os.path.join(DEFAULT_OUT, datetime.now().strftime("%Y%m%d-%H%M%S"))
    paths, md = report.write_all(out_dir, findings, days, rehearsal, github, changes)
    if not args.out or os.path.abspath(out_dir).startswith(os.path.abspath(HOME)):
        _write_private(os.path.join(HOME, "latest-%s.json" % getattr(args, "kind", "scan")),
                       json.dumps({"findings": paths["json"], "html": paths["html"], "generated": findings["meta"]["generated"]}))
    if not args.quiet:
        if rehearse_only and rehearsal:
            start = md.find("## 8. Rehearsal")
            _say(md[start:] if start >= 0 else md)
        elif args.full:
            _say(md)
        else:
            _say("\n".join(report.headline_markdown(findings, days)))
            _say("\nFull report (sections: what you can already show, how your work flows, your definition of done, "
                 "consequential actions, friction, commands I could not place, suggestions%s): %s" % (
                     ", rehearsal" if rehearsal else "", paths["md"]))
    _say("\nReport: %s\nFindings: %s" % (paths["html"], paths["json"]))
    if not actions:
        _say("\nNo agent history found in this window. Checked: %s" % ", ".join(agents))
    return 0


# ---------------------------------------------------------------------------------------------------------------
# rules files


def cmd_init_rules(args):
    out = args.out or os.path.join(os.getcwd(), "scopeblind-rules.json")
    if os.path.exists(out) and not args.force:
        _err("%s already exists; pass --force to replace it." % out)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if args.from_findings:
        findings = _read_json(args.from_findings, {})
        drafts = [s["rule"] for s in findings.get("suggestions", []) if s.get("rule")]
        doc = {"version": 1, "name": "Suggested from my replay", "steps": {}, "budget": {"asks_per_week": 10},
               "rules": drafts}
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    else:
        shutil.copyfile(STARTER, out)
    _say("Wrote %s" % out)
    return 0


def cmd_explain(args):
    doc = rules.load(args.rules)
    bad = rules.problems(doc)
    _say("# %s\n" % doc.get("name"))
    _say("| # | Rule | How it is checked | Live hook |\n|---|---|---|---|")
    for i, row in enumerate(rules.readback(doc), 1):
        live = "enforced" if row["enforced_live"] else ("report only" if row["checked_in_replay"] else "not checked")
        _say("| %d | %s | %s | %s |" % (i, row["says"], row["how"], live))
    if doc.get("live") is False:
        _say("\nThis file says \"live\": false, so no hook enforces it until it is enabled.")
    _say("\nNot covered by any rule: work outside the agents (your own terminal, CI, other people), and anything the "
         "agent's log did not record.")
    if bad:
        _say("\nProblems to fix:\n" + "\n".join("- " + b for b in bad))
        return 2
    return 0


def _edit_rules(path, change):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    change(doc)
    bad = rules.problems(rules.normalize(doc))
    if bad:
        _err("Not saved; the change would leave problems:\n" + "\n".join("  - " + b for b in bad))
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return True


def cmd_label(args):
    kind = args.as_step
    if not re.match(r"^[a-z][a-z0-9_]*$", kind):
        _err("A step name is lower case letters, digits and underscores, for example deploy or smoke_test.")
        return 2
    try:
        rx = re.compile(args.pattern, re.I)
    except re.error as e:
        _err("Not a valid pattern: %s" % e)
        return 2
    since = time.time() - args.days * 86400
    actions = load_actions(since=since, claude_dir=args.claude_dir, codex_dir=args.codex_dir)
    hits, before = [], {}
    for a in actions:
        for seg in rules.action_plan(a) if a.command else []:
            if rx.search(seg.core or seg.raw):
                hits.append((a, seg))
                key = seg.steps[-1] if seg.steps else "none"
                before[key] = before.get(key, 0) + 1
    known = kind in STEPS
    _say("`%s` matches %d commands in the last %d days, in %d sessions." % (
        args.pattern, len(hits), args.days, len({a.session_key for a, _ in hits})))
    if before:
        _say("They were read as: %s." % ", ".join("%s %d" % (k.replace("_", " "), v) for k, v in sorted(before.items(), key=lambda kv: -kv[1])))
    for a, seg in hits[-3:]:
        _say("  e.g. %s: %s" % (disc._day(a.ts), redact(seg.raw, 110)))
    _say("They will count as: %s%s." % (kind, "" if known else " (a new step; refer to it in rules as \"%s\")" % kind))
    if args.dry_run:
        return 0

    def change(doc):
        steps = doc.setdefault("steps", {})
        pats = steps.get(kind) or []
        pats = pats if isinstance(pats, list) else [pats]
        if args.pattern not in pats:
            pats.append(args.pattern)
        steps[kind] = pats
    if _edit_rules(args.rules, change):
        _say("Saved to %s. Rescan to see the effect." % args.rules)
        return 0
    return 2


def cmd_live(args, on):
    if _edit_rules(args.rules, lambda doc: doc.__setitem__("live", on)):
        _say("%s: %s" % (args.rules, "live (hooks enforce it)" if on else "not live (hooks ignore it)"))
        return 0
    return 2


# ---------------------------------------------------------------------------------------------------------------
# install, uninstall, status


def codex_trust_hash(entry, handler_index=0):
    """Codex runs a hook only once it is trusted; this is the hash Codex records for a PreToolUse handler."""
    h = dict(entry["hooks"][handler_index])
    item = {"type": "command", "command": h["command"], "timeout": max(1, int(h.get("timeout") or 600)),
            "async": bool(h.get("async", False))}
    if h.get("statusMessage"):
        item["statusMessage"] = h["statusMessage"]
    if h.get("additionalContextLimit") not in (None, 2500):
        item["additionalContextLimit"] = h["additionalContextLimit"]
    obj = {"event_name": "pre_tool_use", "hooks": [item]}
    if "matcher" in entry:
        obj["matcher"] = entry["matcher"]
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _targets(args):
    agents = ["claude-code", "codex"] if args.agent == "both" else [args.agent]
    project = _git_root(os.getcwd()) if args.scope == "project" else None
    if args.scope == "project" and not project:
        _err("--scope project needs to run inside a git working copy.")
        sys.exit(2)
    out = []
    for agent in agents:
        if agent == "claude-code":
            out.append((agent, os.path.join(project, ".claude", "settings.json") if project else CLAUDE_USER))
        else:
            main = _git_root(project, common=True) if project else None
            out.append((agent, os.path.join(main or project, ".codex", "hooks.json") if project else
                        os.path.join(CODEX_HOME, "hooks.json")))
    return out, project


def _ours(handler):
    return isinstance(handler, dict) and "enforce_hook.py" in str(handler.get("command") or "")


def _without_ours(entries):
    kept = []
    for e in entries or []:
        hooks = [h for h in e.get("hooks") or [] if not _ours(h)]
        if hooks:
            kept.append(dict(e, hooks=hooks))
    return kept


def _show_change(path, before, after, dry_run):
    a = json.dumps(before, indent=2, sort_keys=False).splitlines(True) if before is not None else []
    b = json.dumps(after, indent=2, sort_keys=False).splitlines(True)
    diff = "".join(difflib.unified_diff(a, b, fromfile=path + " (now)", tofile=path + " (after)"))
    _say(diff or "%s: no change needed." % path)
    if dry_run or not diff:
        return False
    if os.path.exists(path):
        backup = "%s.scopeblind-backup-%s" % (path, datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        _say("Backed up the previous file to %s" % backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(after, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return True


def _codex_trust(hooks_path, entry, index, dry_run):
    key = "%s:pre_tool_use:%d:0" % (hooks_path, index)
    value = codex_trust_hash(entry)
    config = os.path.join(CODEX_HOME, "config.toml")
    block = '[hooks.state."%s"]\ntrusted_hash = "%s"\n' % (key.replace("\\", "\\\\").replace('"', '\\"'), value)
    try:
        with open(config, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    header = '[hooks.state."%s"]' % key.replace("\\", "\\\\").replace('"', '\\"')
    if header in text:
        new = re.sub(re.escape(header) + r'\ntrusted_hash = "[^"]*"\n', block.replace("\\", "\\\\"), text)
    else:
        new = text + ("" if text.endswith("\n") or not text else "\n") + "\n" + block
    _say("Codex trust for %s:\n%s" % (hooks_path, block))
    if dry_run or new == text:
        return
    if os.path.exists(config):
        shutil.copy2(config, "%s.scopeblind-backup-%s" % (config, datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(CODEX_HOME, exist_ok=True)
    with open(config, "w", encoding="utf-8") as fh:
        fh.write(new)
    _say("Recorded the trust in %s." % config)


def cmd_install(args):
    targets, project = _targets(args)
    rules_path = os.path.abspath(args.rules or (os.path.join(project, ".claude", "scopeblind-rules.json") if project else USER_RULES))
    if not os.path.isfile(rules_path):
        _err("No rules at %s yet. Write them first (init-rules), then install." % rules_path)
        return 2
    bad = rules.problems(rules.load(rules_path))
    if bad:
        _err("The rules file has problems:\n" + "\n".join("  - " + b for b in bad))
        return 2
    py = sys.executable or "python3"
    for agent, path in targets:
        command = '"%s" "%s" --rules "%s"%s' % (py, HOOK, rules_path, " --agent codex" if agent == "codex" else "")
        before = _read_json(path, None)
        after = json.loads(json.dumps(before or {}))
        if agent == "codex":
            after = {k: v for k, v in after.items() if k in ("description", "hooks")}
            entry = {"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 30}]}
        else:
            entry = {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 30}]}
        hooks = after.setdefault("hooks", {})
        hooks["PreToolUse"] = _without_ours(hooks.get("PreToolUse")) + [entry]
        _say("== %s: %s" % ("Claude Code" if agent == "claude-code" else "Codex", short_path(path)))
        _show_change(path, before, after, args.dry_run)
        if agent == "codex":
            index = len(hooks["PreToolUse"]) - 1
            if args.trust:
                _codex_trust(path, entry, index, args.dry_run)
            else:
                _say("Codex runs a new hook only after you trust it. Start Codex and review it under /hooks, or rerun "
                     "with --trust to record the trust in %s." % short_path(os.path.join(CODEX_HOME, "config.toml")))
    if args.dry_run:
        _say("\nDry run: nothing was written.")
    else:
        _say("\nInstalled. The hook applies %s to every tool call. Turn it off with: python3 \"%s\" uninstall%s" % (
            short_path(rules_path), os.path.abspath(__file__), "" if args.scope == "user" else " --scope project"))
    return 0


def cmd_uninstall(args):
    targets, _ = _targets(args)
    for agent, path in targets:
        before = _read_json(path, None)
        if before is None:
            _say("%s: not installed." % short_path(path))
            continue
        after = json.loads(json.dumps(before))
        hooks = after.get("hooks") or {}
        if "PreToolUse" in hooks:
            hooks["PreToolUse"] = _without_ours(hooks["PreToolUse"])
            if not hooks["PreToolUse"]:
                del hooks["PreToolUse"]
        _say("== %s" % short_path(path))
        _show_change(path, before, after, args.dry_run)
    return 0


def _installed(path):
    data = _read_json(path, None) or {}
    for e in (data.get("hooks") or {}).get("PreToolUse") or []:
        for h in e.get("hooks") or []:
            if _ours(h):
                m = re.search(r'--rules "([^"]+)"', h.get("command") or "")
                return m.group(1) if m else "(rules found by folder)"
    return None


def cmd_status(args):
    from enforce_hook import find_rules
    here = os.getcwd()
    rules_path = find_rules(here)
    _say("Rules in force here: %s" % (short_path(rules_path) if rules_path else "none (write them with init-rules)"))
    if rules_path:
        doc = rules.load(rules_path)
        live_rules = [r for r in doc.get("rules") or [] if r.get("type") in rules.LIVE]
        _say("  %s: %d rules, %d enforced before each call%s" % (doc.get("name"), len(doc.get("rules") or []),
                                                                   len(live_rules), "; switched off (\"live\": false)" if doc.get("live") is False else ""))
    project = _git_root(here)
    places = [("Claude Code, all projects", CLAUDE_USER), ("Codex, all projects", os.path.join(CODEX_HOME, "hooks.json"))]
    if project:
        places += [("Claude Code, this project", os.path.join(project, ".claude", "settings.json")),
                   ("Codex, this project", os.path.join(_git_root(project, common=True) or project, ".codex", "hooks.json"))]
    for name, path in places:
        found = _installed(path)
        _say("%s: %s" % (name, "hook installed (rules: %s)" % short_path(found) if found else "not installed"))
    grants = [g for g in _read_json(os.path.join(STATE, "grants.json"), []) if g.get("until", 0) > time.time()]
    for g in grants:
        _say("Standing approval %s: %s until %s%s" % (g.get("id"), g.get("rule"), datetime.fromtimestamp(g["until"]).strftime("%H:%M %d %b"),
                                                      " (session %s)" % g["session"][:8] if g.get("session") else ""))
    page = _read_json(os.path.join(STATE, "page.json"), None)
    _say("Standard's page: %s" % (page["report"].replace("/api/standard?", "/standard?") if page else "not connected"))
    log = os.path.join(HOME, "decisions.jsonl")
    week = time.time() - 7 * 86400
    counts, recent = {}, []
    try:
        with open(log, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if (d.get("ts") or 0) < week:
                    continue
                key = (d.get("rule"), d.get("decision"))
                counts[key] = counts.get(key, 0) + 1
                recent.append(d)
    except OSError:
        pass
    if counts:
        _say("Decisions in the last 7 days: " + "; ".join("%s %s %d" % (r, {"deny": "refused", "ask": "asked", "covered": "covered by an approval"}.get(d, d), n)
                                                           for (r, d), n in sorted(counts.items(), key=lambda kv: -kv[1])))
        for d in recent[-3:]:
            _say("  %s %s %s: %s" % (d.get("at", "")[:16], d.get("rule"), d.get("decision"), redact(d.get("command") or "", 80)))
    else:
        _say("Decisions in the last 7 days: none recorded.")
    settings = _read_json(os.path.join(HOME, "settings.json"), {})
    _say("Weekly digest: %s" % ("on" if settings.get("weekly") else "off"))
    last = _read_json(os.path.join(HOME, "latest-scan.json"), None)
    if last:
        _say("Last replay: %s (%s)" % (short_path(last.get("html")), (last.get("generated") or "")[:10]))
    return 0


# ---------------------------------------------------------------------------------------------------------------
# approvals and the standard's page


def cmd_approve(args):
    path = os.path.join(STATE, "grants.json")
    grants = _read_json(path, [])
    now = time.time()
    if args.list or not args.rule and not args.revoke and not args.clear:
        active = [g for g in grants if g.get("until", 0) > now]
        _say("\n".join("%s  %s  until %s%s%s" % (g.get("id"), g.get("rule"), datetime.fromtimestamp(g["until"]).strftime("%H:%M %d %b"),
                                                  "  session " + g["session"][:8] if g.get("session") else "",
                                                  "  (%s)" % g["plan"] if g.get("plan") else "") for g in active)
             or "No standing approvals.")
        return 0
    if args.clear:
        _write_private(path, "[]")
        _say("Cleared every standing approval.")
        return 0
    if args.revoke:
        kept = [g for g in grants if g.get("id") != args.revoke]
        _write_private(path, json.dumps(kept))
        _say("Revoked %s." % args.revoke if len(kept) < len(grants) else "No approval with id %s." % args.revoke)
        return 0
    grant = {"id": hashlib.sha256(("%s%s%s" % (args.rule, now, os.getpid())).encode()).hexdigest()[:8],
             "rule": args.rule, "session": args.session, "at": now, "until": now + args.minutes * 60,
             "by": getpass.getuser(), "plan": args.plan, "via": "terminal"}
    grants = [g for g in grants if g.get("until", 0) > now] + [grant]
    _write_private(path, json.dumps(grants))
    _say("Approved %s for %d minutes%s. Id %s (revoke with: approve --revoke %s)." % (
        args.rule, args.minutes, " in session %s" % args.session[:8] if args.session else " in every session", grant["id"], grant["id"]))
    return 0


def cmd_connect(args):
    path = os.path.join(STATE, "page.json")
    if args.disconnect:
        try:
            os.remove(path)
        except OSError:
            pass
        _say("Disconnected: rules with \"approver\": \"page\" now refuse the action until a page is connected.")
        return 0
    m = re.match(r"^(https://[^/]+)/api/standard\?s=([0-9a-f]{24})$", args.report or "")
    if not m:
        _err("The report URL looks like https://scopeblind.com/api/standard?s=<24 characters>. It is shown on the Sign "
             "tab when you publish your standard's page.")
        return 2
    token = sys.stdin.readline().strip() if args.token_stdin else getpass.getpass("Write token (shown once when you published the page; input is hidden): ").strip()
    if not token:
        _err("No token given.")
        return 2
    try:
        from enforce_hook import _http
        status, got = _http(args.report)
    except Exception as e:  # noqa: BLE001
        _err("Could not reach %s: %s" % (args.report, e))
        return 2
    if status != 200 or not got.get("ok"):
        _err("The page answered %s (%s). Check the URL." % (status, got.get("error")))
        return 2
    _write_private(path, json.dumps({"report": args.report, "token": token, "connected_at": time.time()}))
    std = got.get("standard") or {}
    _say("Connected to %s (%s). Rules with \"approver\": \"page\" now hold actions there, for the person named on "
         "the standard to decide." % (args.report.replace("/api/standard?", "/standard?"), std.get("request_id") or "standard"))
    return 0


def _hold_sentence(rule):
    from sbreplay.classify import gerund
    steps = (rule.get("match") or {}).get("steps") or []
    what = rules._join([gerund(s) for s in steps]) if steps else "the actions this rule names"
    return "A person reviews each case of %s before it runs." % what


def cmd_share(args):
    doc = _load_rules(args.rules)
    out = args.out or os.path.join(HOME, "share-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, mode=0o700, exist_ok=True)
    holds = [r for r in doc.get("rules") or [] if r.get("type") == "hold"]
    local = [r for r in doc.get("rules") or [] if r.get("type") in ("block", "require_before", "limit")]
    after = [r for r in doc.get("rules") or [] if r.get("type") == "require_after"]
    judged = [r for r in doc.get("rules") or [] if r.get("type") == "unsupported"]
    L = ["# %s: sharing it" % doc.get("name"), "",
         "## 1. Paste these sentences at https://scopeblind.com/write", "",
         "The work is <say what the agents do, for whom>."]
    L += [_hold_sentence(r) for r in holds]
    L += ["", "Sign it on the Sign tab, then choose \"Publish this standard's page\". Keep the write token it shows "
          "once; do not paste it into a chat.", "",
          "## 2. Connect the page on this machine (in your own terminal, not through the agent)", "",
          "    python3 \"%s\" connect --report 'https://scopeblind.com/api/standard?s=<id>'" % os.path.abspath(__file__), "",
          "Then add \"approver\": \"page\" to each rule below. Those actions then wait on the page, where the person "
          "named on the standard approves or declines the exact command and signs the decision; the agent retries the "
          "same command once they have.", ""]
    L += ["- %s (%s)" % (r.get("says"), r.get("id")) for r in holds] or ["- (no hold rules yet)"]
    L += ["", "## 3. What each side can check", "",
          "On the page: each held command, who decided it, when, and their signature. Anyone with the link can read it.",
          "On this machine only (self-reported by the agent's own log and this hook):"]
    L += ["- " + r.get("says") for r in local] or ["- (none)"]
    if after:
        L += ["", "Reported by the weekly replay, never enforced before the fact:"] + ["- " + r.get("says") for r in after]
    if judged:
        L += ["", "A person judges these; nothing checks them automatically:"] + ["- " + r.get("says") for r in judged]
    L += ["", "The page does not receive the rest of the agent's calls. For a signed receipt of every call, run the "
          "session behind protect-mcp (see the put-a-standard-in-force skill).", "",
          "## 4. Rehearse on the page with your own history (optional)", "",
          "calls.jsonl in this folder is the last %d days of your agents' calls, with secrets redacted. On the Rehearse "
          "tab at /write, load it to see what the standard would have held. It is read in your browser and not "
          "uploaded." % args.days]
    report.write_private(os.path.join(out, "README.md"), "\n".join(L) + "\n")
    since = time.time() - args.days * 86400
    lines = []
    for a in load_actions(since=since, claude_dir=args.claude_dir, codex_dir=args.codex_dir):
        if not a.ts:
            continue
        inp = {"command": redact(a.command, 400)} if a.command else ({"file_path": short_path(a.target)} if a.target else {})
        lines.append(json.dumps({"tool": "Bash" if a.command else a.tool, "input": inp,
                                 "at": datetime.fromtimestamp(a.ts, tz=timezone.utc).isoformat(timespec="seconds"),
                                 "decision": "deny" if a.outcome == "denied" else "allow"}))
    report.write_private(os.path.join(out, "calls.jsonl"), "\n".join(lines) + "\n")
    _say("Wrote %s/README.md (what to paste, how to connect) and calls.jsonl (%d calls, redacted)." % (out, len(lines)))
    return 0


# ---------------------------------------------------------------------------------------------------------------
# the weekly digest


def cmd_digest(args):
    weekly = os.path.join(HOME, "weekly")
    previous = _read_json(os.path.join(HOME, "latest-weekly.json"), None)
    out = os.path.join(weekly, datetime.now().strftime("%Y%m%d"))
    ns = argparse.Namespace(rules=None, since=args.since, days=args.days, agents="claude-code,codex",
                            claude_dir=args.claude_dir, codex_dir=args.codex_dir, project=None, examples=2, github=False, out=out, quiet=True, full=False,
                            compare=(previous or {}).get("findings"), kind="weekly")
    from enforce_hook import find_rules
    found = find_rules(os.getcwd())
    if found and not rules.problems(rules.load(found)):
        ns.rules = found
    cmd_scan(ns)
    findings = _read_json(os.path.join(out, "findings.json"), {})
    h = findings.get("headline") or {}
    L = ["Weekly replay (last %d days):" % args.days, "- " + (h.get("sentence") or "No releases this week.")]
    for c in (findings.get("changes") or [])[:3]:
        L.append("- Since last week: " + c)
    decided = {}
    try:
        with open(os.path.join(HOME, "decisions.jsonl"), "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if (d.get("ts") or 0) >= time.time() - args.days * 86400:
                    decided[d.get("rule")] = decided.get(d.get("rule"), 0) + 1
    except OSError:
        pass
    if decided:
        L.append("- The hook decided %d times: %s." % (sum(decided.values()), ", ".join("%s %d" % kv for kv in sorted(decided.items(), key=lambda kv: -kv[1]))))
    reh = findings.get("rehearsal") or {}
    quiet = [r["id"] for r in reh.get("rules") or [] if r["enforced_live"] and not r["matched"]]
    if quiet:
        L.append("- Rules that never came up: %s." % ", ".join(quiet))
    L.append("- Full report: %s" % os.path.join(out, "report.html"))
    text = "\n".join(L) + "\n"
    report.write_private(os.path.join(out, "digest.md"), text)
    _write_private(os.path.join(HOME, "latest-weekly.json"), json.dumps({
        "findings": os.path.join(out, "findings.json"), "digest": os.path.join(out, "digest.md"),
        "generated": time.time(), "shown": False}))
    if not args.quiet:
        _say(text)
    return 0


def cmd_weekly(args):
    path = os.path.join(HOME, "settings.json")
    settings = _read_json(path, {})
    settings["weekly"] = args.state == "on"
    _write_private(path, json.dumps(settings))
    _say("Weekly digest %s.%s" % (args.state, " With the plugin installed, a new digest is prepared in the background "
                                  "once a week and shown at the start of your next session." if args.state == "on" else ""))
    return 0


# ---------------------------------------------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(prog="replay.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--days", type=int, default=30, help="how many days back to read (default 30)")
        sp.add_argument("--since", help="start date YYYY-MM-DD (overrides --days)")
        sp.add_argument("--project", help="only actions whose working directory contains this text")
        sp.add_argument("--agents", default="claude-code,codex", help="comma list: claude-code,codex")
        sp.add_argument("--claude-dir", help="Claude Code projects directory (default ~/.claude/projects)")
        sp.add_argument("--codex-dir", help="Codex sessions directory (default ~/.codex/sessions)")
        sp.add_argument("--rules", help="rules JSON to rehearse against the history")
        sp.add_argument("--github", action="store_true", help="also read merged pull requests with your gh login (read-only)")
        sp.add_argument("--compare", help="an earlier findings.json to compare against, or 'last'")
        sp.add_argument("--out", help="output directory (default ~/.scopeblind/replay/<timestamp>)")
        sp.add_argument("--examples", type=int, default=3, help="redacted examples per finding (default 3)")
        sp.add_argument("--quiet", action="store_true", help="only print where the report was written")
        sp.add_argument("--full", action="store_true", help="print the whole report, not just the first screen")

    common(sub.add_parser("scan", help="discover how the work flows (and rehearse --rules if given)"))
    common(sub.add_parser("rehearse", help="what a rules file would have done over the history"))
    ini = sub.add_parser("init-rules", help="write starter rules, or draft rules from a findings.json")
    ini.add_argument("--out")
    ini.add_argument("--from", dest="from_findings")
    ini.add_argument("--force", action="store_true")
    exp = sub.add_parser("explain", help="read a rules file back in plain English")
    exp.add_argument("--rules", required=True)
    lab = sub.add_parser("label", help="teach the rules a command of your own (a release script, a smoke test)")
    lab.add_argument("--rules", required=True)
    lab.add_argument("--as", dest="as_step", required=True, help="the step it performs: deploy, publish, test, or a new name")
    lab.add_argument("--pattern", required=True, help="a regular expression for the command")
    lab.add_argument("--days", type=int, default=30)
    lab.add_argument("--dry-run", action="store_true")
    for sp in (lab,):
        sp.add_argument("--claude-dir", help=argparse.SUPPRESS)
        sp.add_argument("--codex-dir", help=argparse.SUPPRESS)
    for name in ("install", "uninstall"):
        sp = sub.add_parser(name, help="%s the hook for Claude Code and/or Codex" % name)
        sp.add_argument("--agent", default="claude-code", choices=("claude-code", "codex", "both"))
        sp.add_argument("--scope", default="user", choices=("user", "project"))
        sp.add_argument("--dry-run", action="store_true")
        if name == "install":
            sp.add_argument("--rules", help="rules file (default: this project's .claude/scopeblind-rules.json or ~/.scopeblind/rules.json)")
            sp.add_argument("--trust", action="store_true", help="also record Codex's trust for the hook in ~/.codex/config.toml")
    sub.add_parser("status", help="what is installed, which rules are in force, recent decisions")
    ap_ = sub.add_parser("approve", help="approve in advance, from your own terminal (Codex cannot ask)")
    ap_.add_argument("rule", nargs="?")
    ap_.add_argument("--minutes", type=int, default=60)
    ap_.add_argument("--session")
    ap_.add_argument("--plan")
    ap_.add_argument("--list", action="store_true")
    ap_.add_argument("--revoke")
    ap_.add_argument("--clear", action="store_true")
    for name in ("enable", "disable"):
        sp = sub.add_parser(name, help="switch a rules file %s" % ("on" if name == "enable" else "off"))
        sp.add_argument("--rules", required=True)
    con = sub.add_parser("connect", help="connect your standard's page on scopeblind.com for page approvals")
    con.add_argument("--report")
    con.add_argument("--token-stdin", action="store_true")
    con.add_argument("--disconnect", action="store_true")
    sh = sub.add_parser("share", help="prepare the standard's page: sentences to paste, and a redacted call log")
    sh.add_argument("--rules", required=True)
    sh.add_argument("--days", type=int, default=30)
    sh.add_argument("--out")
    sh.add_argument("--claude-dir", help=argparse.SUPPRESS)
    sh.add_argument("--codex-dir", help=argparse.SUPPRESS)
    dg = sub.add_parser("digest", help="this week's replay, compared with last week's")
    dg.add_argument("--days", type=int, default=7)
    dg.add_argument("--quiet", action="store_true")
    dg.add_argument("--claude-dir", help=argparse.SUPPRESS)
    dg.add_argument("--codex-dir", help=argparse.SUPPRESS)
    dg.add_argument("--since", help=argparse.SUPPRESS)
    wk = sub.add_parser("weekly", help="turn the weekly digest on or off")
    wk.add_argument("state", choices=("on", "off"))
    sub.add_parser("version")
    args = p.parse_args(argv)
    if args.cmd in ("scan", "rehearse"):
        if args.cmd == "rehearse" and not args.rules:
            p.error("rehearse needs --rules")
        args.kind = "scan"
        return cmd_scan(args, rehearse_only=args.cmd == "rehearse")
    handlers = {"init-rules": cmd_init_rules, "explain": cmd_explain, "label": cmd_label, "install": cmd_install,
                "uninstall": cmd_uninstall, "status": cmd_status, "approve": cmd_approve, "connect": cmd_connect,
                "share": cmd_share, "digest": cmd_digest, "weekly": cmd_weekly,
                "enable": lambda a: cmd_live(a, True), "disable": lambda a: cmd_live(a, False)}
    if args.cmd in handlers:
        return handlers[args.cmd](args)
    if args.cmd == "version":
        _say(__version__)
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
