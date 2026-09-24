"""Write the findings as Markdown (for the chat), JSON (for tools and the next comparison) and HTML (to look at)."""
import html
import json
import os

from .classify import gerund, label, lc, noun


def pct(a, b):
    return "%d%%" % round(100.0 * a / b) if b else "n/a"


def _num(n):
    return "{:,}".format(n) if isinstance(n, int) else str(n)


def _span(minutes):
    if minutes is None:
        return ""
    if minutes < 1:
        return ", usually within a minute"
    return ", typically %s min" % (int(minutes) if minutes >= 10 else minutes)


def compare(current, previous):
    """Key changes since an earlier findings.json."""
    out = []
    cd, pd = current.get("definition_of_done", {}), previous.get("definition_of_done", {})
    for step in ("push", "pr_open", "deploy", "publish"):
        c, p = cd.get(step), pd.get(step)
        if c and p and c["n"] and p["n"]:
            cs, ps = c["tested_final_version"] / float(c["n"]), p["tested_final_version"] / float(p["n"])
            if abs(cs - ps) >= 0.05:
                out.append("%s with the final version tested: %s now, %s before." % (c["label"], pct(c["tested_final_version"], c["n"]),
                                                                                      pct(p["tested_final_version"], p["n"])))
    ca, pa = current.get("after_checks", {}).get("deploy"), previous.get("after_checks", {}).get("deploy")
    if ca and pa and ca["n"] and pa["n"]:
        out.append("Deploys checked afterwards: %s now, %s before." % (pct(ca["checked_after"], ca["n"]), pct(pa["checked_after"], pa["n"])))
    cf, pf = current.get("friction", {}).get("refused", 0), previous.get("friction", {}).get("refused", 0)
    if cf != pf:
        out.append("Refused calls: %s now, %s before." % (_num(cf), _num(pf)))
    for kind, r in current.get("risky", {}).items():
        before = (previous.get("risky", {}).get(kind) or {}).get("ran", 0)
        if r["ran"] != before and (r["ran"] or before):
            out.append("%s that ran: %s now, %s before." % (r["label"], _num(r["ran"]), _num(before)))
    return out


def markdown(f, days, rehearsal=None, github=None, changes=None, files=None):
    o = f["overview"]
    L = []
    agents = ", ".join("%s: %s actions in %s sessions" % (k, _num(v["actions"]), _num(v["sessions"])) for k, v in o["by_agent"].items())
    L.append("# Replay: the last %d days of agent work" % days)
    L.append("")
    L.append("Scanned %s actions (%s), %s to %s, %d active days. Top projects: %s." % (
        _num(o["actions"]), agents or "none found", o["window"]["first_action"], o["window"]["last_action"],
        o["window"]["active_days"], ", ".join(p["project"] for p in o["top_projects"][:3]) or "none"))
    L.append("Everything below comes from the agents' own logs on this machine (self-reported). Nothing was sent anywhere.")
    if changes:
        L.append("")
        L.append("## Since last time")
        L.extend("- " + c for c in changes)
    ev = f["evidence"]
    L.append("")
    L.append("## 1. What you can already show")
    if ev:
        L.append("| Step | Times | Passing test before | Final version tested | CI checked before | Checked after |")
        L.append("|---|---|---|---|---|---|")
        for step, e in ev.items():
            after = "n/a" if e["checked_after"] is None else "%s (%s)" % (_num(e["checked_after"]), pct(e["checked_after"], e["n"]))
            L.append("| %s | %s | %s (%s) | %s (%s) | %s | %s |" % (
                e["label"], _num(e["n"]), _num(e["passing_test_before"]), pct(e["passing_test_before"], e["n"]),
                _num(e["tested_final_version"]), pct(e["tested_final_version"], e["n"]), _num(e["ci_checked_before"]), after))
        L.append("Strength: self-reported. Good for improving your own work; to prove it to someone else, use evidence the agent does not control.")
    else:
        L.append("No pushes, merges, deploys or publishes in this window.")
    if github and github.get("available"):
        L.append("")
        L.append("From GitHub (strong evidence, kept by GitHub):")
        for r in github["repos"]:
            if r.get("error"):
                L.append("- %s: %s" % (r["repo"], r["error"]))
            else:
                L.append("- %s: %s merged pull requests; %s approved by someone other than the author; %s with all checks green; %s both." % (
                    r["repo"], _num(r["merged"]), _num(r["approved_by_someone_else"]), _num(r["checks_green"]), _num(r["approved_and_green"])))
    elif github:
        L.append("")
        L.append("GitHub check skipped: %s" % github.get("reason"))
    L.append("")
    L.append("## 2. How your work flows")
    if f["workflows"]:
        for w in f["workflows"]:
            span = _span(w["median_minutes"])
            L.append("- %s: %s times in %s sessions%s (%s)" % (" -> ".join(w["steps"]), _num(w["times"]), _num(w["sessions"]),
                                                              span, ", ".join(w["projects"])))
    else:
        L.append("Not enough repeated work to show patterns yet.")
    L.append("")
    L.append("## 3. Your definition of done, as practised")
    shown = False
    for step, d in f["definition_of_done"].items():
        if d["n"] < 3:
            continue
        shown = True
        checks = ", ".join("%s %s" % (noun(c), pct(v["present"], d["n"])) for c, v in d["checks_before"].items()
                           if v["share"] >= 0.05) or "no checks"
        line = "- Before %s (%s times): %s. Final version tested %s." % (gerund(step), _num(d["n"]), checks,
                                                                        pct(d["tested_final_version"], d["n"]))
        if d["implicit_done"]:
            line += " Usual pattern: %s; %s went ahead without it." % (" + ".join(noun(c) for c in d["implicit_done"]), _num(d["exceptions"]))
        L.append(line)
    for step, a in f["after_checks"].items():
        shown = True
        L.append("- After %s (%s times): checked within %d minutes %s (%s)." % (
            gerund(step), _num(a["n"]), a["within_minutes"], pct(a["checked_after"], a["n"]), " or ".join(lc(c) for c in a["checked_with"])))
    if not shown:
        L.append("Not enough finished work to infer a definition of done.")
    L.append("")
    L.append("## 4. Consequential actions")
    if f["risky"]:
        L.append("| Action | Attempts | Ran | Refused | Failed | Sessions | Projects |")
        L.append("|---|---|---|---|---|---|---|")
        for k, r in sorted(f["risky"].items(), key=lambda kv: -kv[1]["attempts"]):
            L.append("| %s | %s | %s | %s | %s | %s | %s |" % (r["label"], _num(r["attempts"]), _num(r["ran"]), _num(r["refused"]),
                                                            _num(r["failed"]), _num(r["sessions"]), ", ".join(r["projects"])))
    else:
        L.append("None found.")
    s = f["secrets"]
    if s["commands"]:
        L.append("")
        L.append("Secrets: %s commands in %s sessions carried a literal secret (%s). Values are never shown." % (
            _num(s["commands"]), _num(s["sessions"]), ", ".join("%s %d" % (k, v) for k, v in s["kinds"].items())))
    fr, rw = f["friction"], f["rework"]
    L.append("")
    L.append("## 5. Friction and rework")
    L.append("- Refused calls: %s%s." % (_num(fr["refused"]), (" (most often: " + "; ".join("`%s` %d" % (t["pattern"], t["count"]) for t in fr["top_refused"][:3]) + ")") if fr["refused"] else ""))
    if rw["check_runs"]:
        L.append("- Check runs: %s, failed %s; %s fix-and-rerun loops%s." % (
            _num(rw["check_runs"]), pct(rw["failed_runs"], rw["check_runs"]), _num(rw["fix_and_rerun_loops"]),
            "" if rw["median_attempts_to_pass"] is None else ", usually passing on attempt %s" % rw["median_attempts_to_pass"]))
    if f["automation"]:
        L.append("- Repeated sequences worth a script or skill:")
        for a in f["automation"][:3]:
            L.append("  - %s (%s times, %s sessions)" % (" -> ".join(a["sequence"]), _num(a["times"]), _num(a["sessions"])))
    L.append("")
    L.append("## 6. Suggestions")
    if f["suggestions"]:
        for i, sg in enumerate(f["suggestions"], 1):
            rid = " Draft rule: `%s`." % sg["rule"]["id"] if sg.get("rule") else ""
            L.append("%d. %s. %s%s" % (i, sg["title"], sg["evidence"], rid))
    else:
        L.append("Nothing stands out yet.")
    if rehearsal:
        L.append("")
        L.append("## 7. Rehearsal: %s" % (rehearsal.get("name") or "rules"))
        L.append("| Rule | Matched | Would hold | Would refuse | Missed | Can't tell | Holds a week | Live |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in rehearsal["rules"]:
            L.append("| %s: %s | %s | %s | %s | %s | %s | %s | %s |" % (
                r["id"], r["says"], _num(r["matched"]), _num(r["would_hold"]), _num(r["would_block"]), _num(r["violations"]),
                _num(r["cant_tell"]), r.get("holds_per_week", "n/a"), "yes" if r["enforced_live"] else "report only"))
        for u in rehearsal.get("unsupported") or []:
            L.append("- Not checked (no deterministic check covers it): %s" % u["says"])
        ex = [(r["id"], e) for r in rehearsal["rules"] for e in r["examples"][:2]]
        if ex:
            L.append("")
            L.append("Examples:")
            for rid, e in ex[:8]:
                L.append("- %s, %s %s (%s): `%s` %s" % (rid, e["when"], e["project"], e["session"], e["what"], e.get("why", "")))
    L.append("")
    L.append("## What this cannot see")
    L.append("- Work outside these agents: your own terminal, CI, other people, other tools.")
    L.append("- Results the log did not record are counted as unknown or \"can't tell\", never as passing.")
    L.append("- The log is the agent's own record. It is useful for improving your own work; proof for someone else needs evidence the agent does not control.")
    if files:
        L.append("")
        L.append("Files: " + ", ".join("`%s`" % p for p in files))
    return "\n".join(L) + "\n"


_CSS = """
:root{--bg:#fbfaf7;--card:#ffffff;--ink:#1c2321;--dim:#5d6863;--line:#e4e1d9;--accent:#0b7369;--accent-soft:#e3f1ee;
--warn:#a8621a;--warn-soft:#fbefe1;--bad:#a33a2c;--bad-soft:#f9e6e2;--good:#2f7a3f;--good-soft:#e5f2e7}
@media (prefers-color-scheme:dark){:root{--bg:#121615;--card:#1a201e;--ink:#e8ecea;--dim:#9aa6a1;--line:#2b3431;
--accent:#4fc1b3;--accent-soft:#16312d;--warn:#e0a35e;--warn-soft:#33271a;--bad:#ea8b7d;--bad-soft:#3a1f1b;--good:#7fcf8e;--good-soft:#1c3122}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 20px 64px}h1{font-size:28px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:18px;margin:36px 0 12px}p.dim,.dim{color:var(--dim)}.note{font-size:13px;color:var(--dim)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}.stat b{display:block;font-size:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:10px 0}
.flow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:4px 0 8px}.chip{padding:3px 10px;border-radius:999px;
background:var(--accent-soft);color:var(--accent);font-size:13px;font-weight:600}.chip.finish{background:var(--good-soft);color:var(--good)}
.chip.risky{background:var(--warn-soft);color:var(--warn)}.arrow{color:var(--dim)}
.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.rows{display:grid;grid-template-columns:minmax(120px,170px) 1fr 44px;gap:6px 12px;align-items:center;margin:10px 0 6px;font-size:14px}
.rows span:last-child{text-align:right;font-variant-numeric:tabular-nums}.rows .final{font-weight:600}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}.wrap{overflow-x:auto}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}.pill.hold{background:var(--warn-soft);color:var(--warn)}
.pill.block{background:var(--bad-soft);color:var(--bad)}.pill.ok{background:var(--good-soft);color:var(--good)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:var(--accent-soft);padding:1px 5px;border-radius:5px;word-break:break-all}
ol li{margin:6px 0}
"""


def _chip(kind):
    from .classify import category
    cat = category(kind)
    cls = "finish" if cat == "finish" else "risky" if cat == "risky" else ""
    return '<span class="chip %s">%s</span>' % (cls, html.escape(label(kind)))


def html_report(f, days, rehearsal=None, github=None, changes=None):
    e = html.escape
    o = f["overview"]
    P = []
    P.append('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    P.append("<title>Agent work replay</title><style>%s</style></head><body><main>" % _CSS)
    P.append("<h1>How your agents actually work</h1>")
    P.append('<p class="dim">The last %d days, %s to %s. Read from the agents\' own logs on this machine; nothing was sent anywhere.</p>' % (
        days, e(o["window"]["first_action"]), e(o["window"]["last_action"])))
    P.append('<div class="stats">')
    P.append('<div class="stat"><b>%s</b>actions</div>' % _num(o["actions"]))
    for k, v in o["by_agent"].items():
        P.append('<div class="stat"><b>%s</b>%s sessions</div>' % (_num(v["sessions"]), e(k)))
    P.append('<div class="stat"><b>%s</b>active days</div>' % _num(o["window"]["active_days"]))
    P.append('<div class="stat"><b>%s</b>refused calls</div></div>' % _num(f["friction"]["refused"]))
    if changes:
        P.append("<h2>Since last time</h2><ul>%s</ul>" % "".join("<li>%s</li>" % e(c) for c in changes))
    P.append("<h2>What you can already show</h2>")
    if f["evidence"]:
        P.append('<div class="wrap"><table><tr><th>Step</th><th>Times</th><th>Passing test before</th><th>Final version tested</th><th>Checked after</th></tr>')
        for step, ev in f["evidence"].items():
            after = "n/a" if ev["checked_after"] is None else pct(ev["checked_after"], ev["n"])
            P.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                e(ev["label"]), _num(ev["n"]), pct(ev["passing_test_before"], ev["n"]), pct(ev["tested_final_version"], ev["n"]), after))
        P.append('</table></div><p class="note">Self-reported by the agent. To prove it to someone else, use evidence the agent does not control.</p>')
    if github and github.get("available"):
        for r in github["repos"]:
            if not r.get("error"):
                P.append('<div class="card"><b>%s</b> (GitHub, strong): %s merged; %s approved by someone else; %s with green checks.</div>' % (
                    e(r["repo"]), _num(r["merged"]), _num(r["approved_by_someone_else"]), _num(r["checks_green"])))
    P.append("<h2>How your work flows</h2>")
    for w in f["workflows"]:
        chips = '<span class="arrow">→</span>'.join(_chip(k) for k in w["kinds"])
        span = _span(w["median_minutes"])
        P.append('<div class="card"><div class="flow">%s</div><div class="dim">%s times in %s sessions%s · %s</div></div>' % (
            chips, _num(w["times"]), _num(w["sessions"]), span, e(", ".join(w["projects"]))))
    if not f["workflows"]:
        P.append('<p class="dim">Not enough repeated work to show patterns yet.</p>')
    P.append("<h2>Your definition of done, as practised</h2>")
    for step, d in f["definition_of_done"].items():
        if d["n"] < 3:
            continue
        P.append('<div class="card"><b>Before %s</b> <span class="dim">(%s times)</span><div class="rows">' % (e(gerund(step)), _num(d["n"])))
        for c, v in d["checks_before"].items():
            if v["share"] < 0.05:
                continue
            P.append('<span class="dim">%s</span><div class="bar"><i style="width:%s"></i></div><span>%s</span>' % (
                e(label(c)), pct(v["present"], d["n"]), pct(v["present"], d["n"])))
        P.append('<span class="final">Final version tested</span><div class="bar"><i style="width:%s"></i></div><span class="final">%s</span></div>' % (
            pct(d["tested_final_version"], d["n"]), pct(d["tested_final_version"], d["n"])))
        if d["implicit_done"]:
            P.append("<div>Usual pattern: <b>%s</b>; %s went ahead without it.</div>" % (
                e(" + ".join(noun(c) for c in d["implicit_done"])), _num(d["exceptions"])))
        P.append("</div>")
    for step, a in f["after_checks"].items():
        P.append('<div class="card"><b>After %s</b> <span class="dim">(%s times)</span><div class="rows"><span class="dim">Checked within %d min</span><div class="bar"><i style="width:%s"></i></div><span>%s</span></div></div>' % (
            e(gerund(step)), _num(a["n"]), a["within_minutes"], pct(a["checked_after"], a["n"]), pct(a["checked_after"], a["n"])))
    if f["risky"]:
        P.append('<h2>Consequential actions</h2><div class="wrap"><table><tr><th>Action</th><th>Attempts</th><th>Ran</th><th>Refused</th><th>Sessions</th></tr>')
        for k, r in sorted(f["risky"].items(), key=lambda kv: -kv[1]["attempts"]):
            P.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                e(r["label"]), _num(r["attempts"]), _num(r["ran"]), _num(r["refused"]), _num(r["sessions"])))
        P.append("</table></div>")
    if f["suggestions"]:
        P.append("<h2>Suggestions</h2><ol>")
        for sg in f["suggestions"]:
            P.append("<li><b>%s.</b> <span class=\"dim\">%s</span></li>" % (e(sg["title"]), e(sg["evidence"])))
        P.append("</ol>")
    if rehearsal:
        P.append('<h2>Rehearsal</h2><div class="wrap"><table><tr><th>Rule</th><th>Would hold</th><th>Would refuse</th><th>Missed</th><th>Can\'t tell</th><th>Live</th></tr>')
        for r in rehearsal["rules"]:
            P.append('<tr><td><b>%s</b><br><span class="dim">%s</span></td><td><span class="pill hold">%s</span></td><td><span class="pill block">%s</span></td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                e(r["id"]), e(r["says"] or ""), _num(r["would_hold"]), _num(r["would_block"]), _num(r["violations"]), _num(r["cant_tell"]),
                "yes" if r["enforced_live"] else "report only"))
        P.append("</table></div>")
    P.append("<h2>What this cannot see</h2><ul><li>Work outside these agents: your own terminal, CI, other people, other tools.</li>"
             "<li>Results the log did not record are counted as unknown, never as passing.</li>"
             "<li>The log is the agent's own record. Proof for someone else needs evidence the agent does not control.</li></ul>")
    P.append("</main></body></html>")
    return "".join(P)


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_all(out_dir, findings, days, rehearsal=None, github=None, changes=None):
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    paths = {k: os.path.join(out_dir, v) for k, v in (("json", "findings.json"), ("md", "report.md"), ("html", "report.html"))}
    doc = dict(findings)
    doc["rehearsal"] = rehearsal
    doc["github"] = github
    doc["changes"] = changes
    write_private(paths["json"], json.dumps(doc, indent=2, default=str))
    md = markdown(findings, days, rehearsal, github, changes, files=[paths["html"], paths["json"]])
    write_private(paths["md"], md)
    write_private(paths["html"], html_report(findings, days, rehearsal, github, changes))
    return paths, md
