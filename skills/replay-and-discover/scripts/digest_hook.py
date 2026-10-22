#!/usr/bin/env python3
"""SessionStart hook: show the weekly replay digest once, and prepare the next one in the background.

Does nothing unless the person turned the digest on (`replay.py weekly on`). Never slows the session: preparing a
digest runs in a separate process, and this hook only reads two small files.
"""
import json
import os
import subprocess
import sys
import time

HOME = os.environ.get("SCOPEBLIND_REPLAY_HOME") or os.path.expanduser("~/.scopeblind/replay")
REPLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay.py")
WEEK = 7 * 86400


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def main():
    try:
        sys.stdin.read()
        if not _read(os.path.join(HOME, "settings.json"), {}).get("weekly"):
            return 0
        latest_path = os.path.join(HOME, "latest-weekly.json")
        latest = _read(latest_path, None)
        if latest and not latest.get("shown") and os.path.isfile(latest.get("digest") or ""):
            with open(latest["digest"], "r", encoding="utf-8") as fh:
                digest = fh.read().strip()
            sys.stdout.write("The replay-and-discover weekly digest is ready. If it is useful now, tell the person in "
                             "one or two sentences and offer the full report:\n" + digest + "\n")
            latest["shown"] = True
            with open(latest_path, "w", encoding="utf-8") as fh:
                json.dump(latest, fh)
        marker = os.path.join(HOME, "state", ".digest-started")
        started = os.path.getmtime(marker) if os.path.exists(marker) else 0
        if (not latest or time.time() - latest.get("generated", 0) >= WEEK) and time.time() - started > 3600:
            os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
            open(marker, "w").close()
            with open(os.devnull, "wb") as null:
                subprocess.Popen([sys.executable, REPLAY, "digest", "--quiet"], stdin=null, stdout=null, stderr=null,
                                 cwd=os.path.expanduser("~"), start_new_session=True)
    except Exception as e:  # never break a session
        sys.stderr.write("replay-and-discover digest skipped: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
