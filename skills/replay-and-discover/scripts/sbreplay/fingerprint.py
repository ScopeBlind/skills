"""Fingerprint a git working copy, to tell whether the files a check ran on are the files being shipped.

The live hook takes a snapshot when a test (or another required check) is about to run, and compares it with the
working copy when a push or deploy is about to run. The snapshot is what the working copy holds relative to HEAD:
the commit, plus a content hash (git's own blob id) for every changed, added, deleted or untracked file. Nothing is
written to the repository: git is only asked for `status`, `diff-tree` and `cat-file` output.

Comparing two snapshots gives the exact files whose content differs, whatever changed them: an agent's edit tool, a
shell command, another agent, or a person. Committing the tested files does not count as a change; editing them does.
For a push, the pushed content is HEAD, so files that were never committed are left out.
"""
import fnmatch
import hashlib
import os
import subprocess

MAX_BYTES = 256 * 1024 * 1024
MAX_UNTRACKED = 5000


def _git(root, args, stdin=None, timeout=15):
    try:
        p = subprocess.run(["git", "-C", root, "--no-optional-locks"] + args, input=stdin, capture_output=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


_ROOTS = {}


def repo_root(path):
    """The top folder of the git working copy that holds `path`, or None."""
    if not path:
        return None
    if path in _ROOTS:
        return _ROOTS[path]
    out = _git(path, ["rev-parse", "--show-toplevel"], timeout=5) if os.path.isdir(path) else None
    root = os.path.normpath(out.decode("utf-8", "surrogateescape").strip()) if out else None
    _ROOTS[path] = root
    return root


def _object_format(root):
    out = _git(root, ["rev-parse", "--show-object-format"], timeout=5)
    fmt = out.decode().strip() if out else "sha1"
    return fmt if fmt in ("sha1", "sha256") else "sha1"


def blob_id(path, fmt="sha1"):
    """git's blob id for a file's current content (a symlink's target for a link), or None if unreadable."""
    try:
        if os.path.islink(path):
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        else:
            with open(path, "rb") as fh:
                data = fh.read()
    except OSError:
        return None
    h = hashlib.new(fmt)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def snapshot(root):
    """What the working copy at `root` holds, relative to HEAD. None if it is not a readable git working copy, or if
    it is too large to hash quickly."""
    if not root or not os.path.isdir(root):
        return None
    head_out = _git(root, ["rev-parse", "--verify", "-q", "HEAD"], timeout=5)
    head = head_out.decode().strip() if head_out else None
    status = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"],
                  timeout=30)
    if status is None:
        return None
    fmt = _object_format(root)
    changed, untracked, total = {}, {}, 0
    entries = status.split(b"\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy = entry[:2].decode("ascii", "replace")
        path = entry[3:].decode("utf-8", "surrogateescape")
        if xy[0] in "RC" and i < len(entries):
            source = entries[i].decode("utf-8", "surrogateescape")
            i += 1
            if xy[0] == "R":
                changed[source] = None
        full = os.path.join(root, path)
        if os.path.isdir(full) and not os.path.islink(full):
            continue  # a nested repository or submodule
        exists = os.path.lexists(full)
        if exists and not os.path.islink(full):
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
            if total > MAX_BYTES:
                return None
        if xy == "??":
            if len(untracked) >= MAX_UNTRACKED:
                return None
            untracked[path] = blob_id(full, fmt) if exists else None
        else:
            changed[path] = blob_id(full, fmt) if exists else None
    return {"root": root, "head": head, "format": fmt, "changed": changed, "untracked": untracked}


def _ignored(path, patterns):
    for pat in patterns or ():
        p = pat.rstrip("/")
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(os.path.basename(path), pat):
            return True
        if path == p or path.startswith(p + "/"):
            return True
        if p.endswith("/**") and path.startswith(p[:-3] + "/"):
            return True
    return False


def _head_blobs(root, head, paths):
    """{path: blob id or None} for `paths` in commit `head` (None: the file is not in that commit)."""
    if not head or not paths:
        return {p: None for p in paths}
    lines = [p for p in paths if "\n" not in p]
    out = _git(root, ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
               stdin=("".join("%s:%s\n" % (head, p) for p in lines)).encode("utf-8", "surrogateescape"), timeout=30)
    if out is None:
        return None
    result = {}
    for p, line in zip(lines, out.decode("utf-8", "surrogateescape").split("\n")):
        parts = line.split()
        result[p] = parts[0] if len(parts) == 2 and parts[1] == "blob" else None
    for p in paths:
        result.setdefault(p, None)
    return result


def compare(then, now, mode="worktree", ignore=()):
    """Whether the content `then` saw is the content `now` holds.

    mode "worktree": everything in the working copy (for deploys, publishes, builds). mode "head": what is committed
    now, which is what a push sends (files that were never committed are left out).
    Returns ("same", []), ("changed", [paths]) or (None, reason)."""
    if not then or not now:
        return None, "no snapshot"
    if then.get("root") != now.get("root"):
        return None, "a different folder"
    if then.get("format") != now.get("format"):
        return None, "a different object format"
    root = now["root"]
    paths = set(then["changed"]) | set(then["untracked"])
    if mode == "worktree":
        paths |= set(now["changed"]) | set(now["untracked"])
    if then.get("head") != now.get("head"):
        if then.get("head") and now.get("head"):
            diff = _git(root, ["diff-tree", "-r", "-z", "--no-renames", "--no-commit-id", "--name-only",
                               then["head"], now["head"]], timeout=30)
            if diff is None:
                return None, "the earlier commit is no longer readable"
            paths |= {p.decode("utf-8", "surrogateescape") for p in diff.split(b"\0") if p}
        else:
            return None, "no earlier commit to compare with"
    paths = sorted(p for p in paths if not _ignored(p, ignore))
    old = _head_blobs(root, then.get("head"), paths)
    new = _head_blobs(root, now.get("head"), paths)
    if old is None or new is None:
        return None, "git could not be read"
    changed = []
    for p in paths:
        if p in then["changed"]:
            seen = then["changed"][p]
        elif p in then["untracked"]:
            seen = then["untracked"][p]
        else:
            seen = old.get(p)
        if mode == "head":
            ships = new.get(p)
            if p in then["untracked"] and ships is None:
                continue
        elif p in now["changed"]:
            ships = now["changed"][p]
        elif p in now["untracked"]:
            ships = now["untracked"][p]
        else:
            ships = new.get(p)
        if seen != ships:
            changed.append(p)
    return ("changed", changed) if changed else ("same", [])
