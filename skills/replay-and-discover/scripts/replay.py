#!/usr/bin/env python3
"""Replay an agent's local history, discover how the work flows, and rehearse rules against it.

    replay.py scan      [--days 30] [--project TEXT] [--agents claude-code,codex] [--rules FILE] [--github] [--compare FILE]
    replay.py rehearse  --rules FILE [same options as scan]
    replay.py init-rules [--out FILE] [--from FINDINGS_JSON] [--force]
    replay.py explain   --rules FILE

Reads ~/.claude/projects and ~/.codex/sessions on this machine. Writes findings.json, report.md and report.html to
~/.scopeblind/replay/<timestamp>/ (private to you). Sends nothing anywhere; --github reads merged pull requests with
your own `gh` login and is off unless you pass it.
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sbreplay import __version__  # noqa: E402
from sbreplay import discover as disc  # noqa: E402
from sbreplay import report, rules  # noqa: E402
from sbreplay.sources import load_actions  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STARTER = os.path.join(os.path.dirname(HERE), "assets", "starter-rules.json")
DEFAULT_OUT = os.path.expanduser("~/.scopeblind/replay")


def _window(args):
    now = time.time()
    until = now
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    else:
        since = now - args.days * 86400
    days = max(1, int(round((until - since) / 86400.0)))
    return since, until, days


def _load_rules(path):
    if not path:
        return None
    doc = rules.load(path)
    bad = rules.problems(doc)
    if bad:
        sys.stderr.write("The rules file has problems:\n" + "\n".join("  - " + b for b in bad) + "\n")
        sys.exit(2)
    return doc


def cmd_scan(args, rehearse_only=False):
    doc = _load_rules(args.rules)
    since, until, days = _window(args)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    t0 = time.time()
    actions = load_actions(agents=agents, since=since, until=until, claude_dir=args.claude_dir,
                           codex_dir=args.codex_dir, extra=(doc or {}).get("_extra"), project=args.project)
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
    if args.compare:
        try:
            with open(args.compare, "r", encoding="utf-8") as fh:
                changes = report.compare(findings, json.load(fh))
        except (OSError, ValueError) as e:
            sys.stderr.write("Could not read %s for comparison: %s\n" % (args.compare, e))
    out_dir = args.out or os.path.join(DEFAULT_OUT, datetime.now().strftime("%Y%m%d-%H%M%S"))
    paths, md = report.write_all(out_dir, findings, days, rehearsal, github, changes)
    if not args.quiet:
        if rehearse_only and rehearsal:
            start = md.find("## 7. Rehearsal")
            sys.stdout.write(md[start:] if start >= 0 else md)
        else:
            sys.stdout.write(md)
    sys.stdout.write("\nReport: %s\nFindings: %s\n" % (paths["html"], paths["json"]))
    if not actions:
        sys.stdout.write("\nNo agent history found in this window. Checked: %s\n" % ", ".join(agents))
    return 0


def cmd_init_rules(args):
    out = args.out or os.path.join(os.getcwd(), "scopeblind-rules.json")
    if os.path.exists(out) and not args.force:
        sys.stderr.write("%s already exists; pass --force to replace it.\n" % out)
        return 2
    if args.from_findings:
        with open(args.from_findings, "r", encoding="utf-8") as fh:
            findings = json.load(fh)
        drafts = [s["rule"] for s in findings.get("suggestions", []) if s.get("rule")]
        doc = {"version": 1, "name": "Suggested from my replay", "steps": {}, "rules": drafts}
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
    else:
        shutil.copyfile(STARTER, out)
    sys.stdout.write("Wrote %s\n" % out)
    return 0


def cmd_explain(args):
    doc = rules.load(args.rules)
    bad = rules.problems(doc)
    sys.stdout.write("# %s\n\n" % doc.get("name"))
    sys.stdout.write("| # | Rule | How it is checked | Live hook |\n|---|---|---|---|\n")
    for i, row in enumerate(rules.readback(doc), 1):
        live = "enforced" if row["enforced_live"] else ("report only" if row["checked_in_replay"] else "not checked")
        sys.stdout.write("| %d | %s | %s | %s |\n" % (i, row["says"], row["how"], live))
    sys.stdout.write("\nNot covered by any rule: work outside the agent (your own terminal, CI, other people), and anything "
                     "the agent's log did not record.\n")
    if bad:
        sys.stdout.write("\nProblems to fix:\n" + "\n".join("- " + b for b in bad) + "\n")
        return 2
    return 0


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
        sp.add_argument("--compare", help="an earlier findings.json to compare against")
        sp.add_argument("--out", help="output directory (default ~/.scopeblind/replay/<timestamp>)")
        sp.add_argument("--examples", type=int, default=3, help="redacted examples per finding (default 3)")
        sp.add_argument("--quiet", action="store_true", help="only print where the report was written")

    common(sub.add_parser("scan", help="discover how the work flows (and rehearse --rules if given)"))
    reh = sub.add_parser("rehearse", help="what a rules file would have done over the history")
    common(reh)
    ini = sub.add_parser("init-rules", help="write starter rules, or draft rules from a findings.json")
    ini.add_argument("--out")
    ini.add_argument("--from", dest="from_findings")
    ini.add_argument("--force", action="store_true")
    exp = sub.add_parser("explain", help="read a rules file back in plain English")
    exp.add_argument("--rules", required=True)
    sub.add_parser("version")
    args = p.parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "rehearse":
        if not args.rules:
            p.error("rehearse needs --rules")
        return cmd_scan(args, rehearse_only=True)
    if args.cmd == "init-rules":
        return cmd_init_rules(args)
    if args.cmd == "explain":
        return cmd_explain(args)
    if args.cmd == "version":
        print(__version__)
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
