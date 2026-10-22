"""Optional: stronger evidence from GitHub, read with the person's own `gh` login.

The agent's log is self-reported. GitHub's record of a merged pull request (who approved it, whether its checks
passed) is kept by a party the agent does not control, so it is stronger evidence. This module only reads, only runs
when the person asks for it, and reports counts, never titles or code.
"""
import json
import os
import re
import subprocess

_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _run(args, timeout=30, cwd=None):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def repos_for(projects, limit=15):
    """GitHub owner/repo names for the working directories seen in the history (local `git` only)."""
    found = []
    for path in projects:
        if not path or not os.path.isdir(path):
            continue
        url = _run(["git", "-C", path, "remote", "get-url", "origin"], timeout=5)
        m = _REMOTE.search((url or "").strip())
        if m:
            name = "%s/%s" % (m.group(1), m.group(2))
            if name not in found:
                found.append(name)
        if len(found) >= limit:
            break
    return found


def _checks_green(rollup):
    if not rollup:
        return None
    for c in rollup:
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state not in _GREEN:
            return False
    return True


def merged_evidence(repos, since_date, limit=100):
    """Per repository: merged pull requests since `since_date`, with independent approval and green checks."""
    if not _run(["gh", "--version"], timeout=10):
        return {"available": False, "reason": "The GitHub CLI (gh) is not installed or not on PATH."}
    if _run(["gh", "auth", "status"], timeout=15) is None:
        return {"available": False, "reason": "The GitHub CLI is not signed in (run: gh auth login)."}
    rows = []
    for repo in repos:
        raw = _run(["gh", "pr", "list", "--repo", repo, "--state", "merged", "--search", "merged:>=%s" % since_date,
                    "--limit", str(limit), "--json", "number,author,reviews,statusCheckRollup"], timeout=60)
        if raw is None:
            rows.append({"repo": repo, "error": "could not read (no access, or the repository is gone)"})
            continue
        try:
            prs = json.loads(raw)
        except ValueError:
            rows.append({"repo": repo, "error": "unexpected response"})
            continue
        approved = green = both = no_checks = 0
        for pr in prs:
            author = ((pr.get("author") or {}).get("login") or "").lower()
            ok_review = any((r.get("state") == "APPROVED" and ((r.get("author") or {}).get("login") or "").lower() != author)
                            for r in pr.get("reviews") or [])
            checks = _checks_green(pr.get("statusCheckRollup"))
            approved += 1 if ok_review else 0
            green += 1 if checks else 0
            no_checks += 1 if checks is None else 0
            both += 1 if (ok_review and checks) else 0
        rows.append({"repo": repo, "merged": len(prs), "approved_by_someone_else": approved, "checks_green": green,
                     "no_checks": no_checks, "approved_and_green": both})
    return {"available": True, "since": since_date, "repos": rows,
            "strength": "strong (kept by GitHub, which the agent does not control)"}
