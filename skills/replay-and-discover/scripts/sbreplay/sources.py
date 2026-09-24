"""Read local agent history into one list of actions.

Supported:
- Claude Code: ~/.claude/projects/**/*.jsonl (or $CLAUDE_CONFIG_DIR/projects). Tool calls are `tool_use` items in
  assistant entries; outcomes are the matching `tool_result` items (is_error, "Exit code N", denials).
- Codex CLI: ~/.codex/sessions/**/*.jsonl (or $CODEX_HOME/sessions). Older sessions log `exec_command` function calls
  whose output carries "Process exited with code N" and "Wall time: N seconds". Newer sessions log `item_completed`
  events: CommandExecution (command, cwd, exit_code, duration), FileChange (paths), McpToolCall (server, tool, status).

Files are streamed line by line; nothing is written anywhere and nothing is sent anywhere.
"""
import glob
import json
import os
import re
from datetime import datetime, timezone
try:
    from urllib.parse import unquote, urlparse
except ImportError:  # pragma: no cover
    from urllib import unquote  # type: ignore
    from urlparse import urlparse  # type: ignore

from .classify import tool_steps

EXIT_CODE = re.compile(r"Process exited with code (-?\d+)")
WALL_TIME = re.compile(r"Wall time: ([0-9.]+) seconds")
CLAUDE_EXIT = re.compile(r"^\s*Exit code (-?\d+)")
DENIED = re.compile(
    r"(?i)permission to use \S+ (?:has been|was) denied|was denied by|\bdenied by\b|\[scopeblind\] denied|"
    r"doesn't want to proceed|user rejected|blocked by (?:a |the )?(?:hook|policy|classifier)|hook (?:error|blocked)|"
    r"pretooluse:\S+ hook|^<tool_use_error>blocked\b|^blocked:|\bnot permitted by\b|\brefused by\b"
)


class Action(object):
    """One tool call an agent made."""
    __slots__ = ("id", "agent", "session", "subagent", "project", "branch", "ts", "tool", "command", "target",
                 "steps", "outcome", "exit_code", "duration", "order")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        if isinstance(self.project, str) and self.project.startswith("file://"):
            self.project = unquote(urlparse(self.project).path) or self.project
        if self.outcome is None:
            self.outcome = "unknown"
        if self.steps is None:
            self.steps = []

    @property
    def session_key(self):
        return "%s:%s" % (self.agent, self.session)

    def ran(self):
        """True unless the call was refused before it ran."""
        return self.outcome not in ("denied",)


def parse_ts(value):
    """ISO 8601 string or epoch number -> epoch seconds (float), or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    if isinstance(value, str):
        v = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", v)
            if not m:
                return None
            dt = datetime.fromisoformat(m.group(1) + "+00:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict) and isinstance(x.get("text"), str))
    return ""


def claude_root(explicit=None):
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects")


def codex_root(explicit=None):
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(base, "sessions")


def _files(root, since):
    if not os.path.isdir(root):
        return []
    out = []
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        name = os.path.basename(path)
        if name == "journal.jsonl":
            continue
        try:
            if since is not None and os.path.getmtime(path) < since:
                continue
        except OSError:
            continue
        out.append(path)
    return sorted(out)


def _in_window(ts, since, until):
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def _claude_target(tool, inp):
    if not isinstance(inp, dict):
        return None
    for key in ("file_path", "notebook_path", "path", "url", "pattern", "query", "skill", "description"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def read_claude(root=None, since=None, until=None, extra=None, seen=None):
    """Yield Actions from Claude Code transcripts."""
    seen = set() if seen is None else seen
    for path in _files(claude_root(root), since):
        for act in read_claude_file(path, since, until, extra, seen):
            yield act


def _lines(path, tail_bytes=None):
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        if tail_bytes:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # drop the partial first line
            else:
                fh.seek(0)
        for raw in fh:
            yield raw.decode("utf-8", "replace")


def read_claude_file(path, since=None, until=None, extra=None, seen=None, tail_bytes=None):
    """Yield Actions from one Claude Code transcript (optionally only its last `tail_bytes`)."""
    seen = set() if seen is None else seen
    pending = {}
    fallback_session = os.path.splitext(os.path.basename(path))[0]
    try:
        lines = list(_lines(path, tail_bytes)) if tail_bytes else _lines(path)
    except OSError:
        return
    for line in lines:
        if '"tool_use"' not in line and '"tool_result"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        kind = entry.get("type")
        ts = parse_ts(entry.get("timestamp"))
        if kind == "assistant":
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                tid = item.get("id")
                if not tid or tid in seen or not _in_window(ts, since, until):
                    continue
                seen.add(tid)
                tool = item.get("name") or "?"
                inp = item.get("input") if isinstance(item.get("input"), dict) else {}
                command = inp.get("command") if tool == "Bash" else None
                pending[tid] = Action(
                    id=tid, agent="claude-code", session=entry.get("sessionId") or fallback_session,
                    subagent=bool(entry.get("isSidechain")), project=entry.get("cwd"),
                    branch=entry.get("gitBranch"), ts=ts, tool=tool, command=command,
                    target=_claude_target(tool, inp), steps=tool_steps(tool, command, extra),
                )
        elif kind == "user":
            tur = entry.get("toolUseResult") if isinstance(entry.get("toolUseResult"), dict) else {}
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                act = pending.pop(item.get("tool_use_id"), None)
                if act is None:
                    continue
                _claude_outcome(act, item, tur)
                if ts is not None and act.ts is not None and ts >= act.ts:
                    act.duration = ts - act.ts
                yield act
    for act in pending.values():
        yield act


def _claude_outcome(act, item, tur):
    text = _result_text(item.get("content")) or ""
    if tur.get("interrupted"):
        act.outcome = "interrupted"
    elif not item.get("is_error"):
        act.outcome = "ok"
        if act.tool == "Bash":
            act.exit_code = 0
    else:
        m = CLAUDE_EXIT.match(text)
        if m:
            act.outcome, act.exit_code = "failed", int(m.group(1))
        elif DENIED.search(text):
            act.outcome = "denied"
        else:
            act.outcome = "error"


def _cmd_text(command):
    if isinstance(command, list):
        parts = [str(p) for p in command]
        if len(parts) >= 3 and os.path.basename(parts[0]) in ("bash", "zsh", "sh", "dash") and parts[1] in ("-lc", "-c"):
            return parts[2]
        return " ".join(parts)
    return command if isinstance(command, str) else None


def _patch_paths(text):
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", text or "", re.M)


_CODEX_ITEMS = ('"CommandExecution"', '"FileChange"', '"McpToolCall"', '"WebSearch"')
_CODEX_ITEM_TYPES = ("CommandExecution", "FileChange", "McpToolCall", "WebSearch")
_TYPE = re.compile(r'"type"\s*:\s*"(\w+)"')
_CALL_ID = re.compile(r'"call_id"\s*:\s*"([^"]+)"')


def _fast_output(line, pending):
    """Read a function_call_output without parsing its (often very large) JSON: call id, exit code, wall time."""
    m = _CALL_ID.search(line)
    act = pending.pop(m.group(1), None) if m else None
    if act is None:
        return
    code = EXIT_CODE.search(line)
    if code:
        act.exit_code = int(code.group(1))
        act.outcome = "ok" if act.exit_code == 0 else "failed"
    elif act.tool != "shell":
        act.outcome = "ok"
    w = WALL_TIME.search(line)
    if w:
        act.duration = float(w.group(1))


def read_codex(root=None, since=None, until=None, extra=None, seen=None):
    """Yield Actions from Codex CLI session files (both the older and the newer formats)."""
    seen = set() if seen is None else seen
    for path in _files(codex_root(root), since):
        session = os.path.splitext(os.path.basename(path))[0]
        cwd, subagent = None, False
        old, new, pending = [], [], {}
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                kinds = _TYPE.findall(line[:600])
                if not kinds:
                    continue
                rec, sub = kinds[0], (kinds[1] if len(kinds) > 1 else None)
                if rec == "response_item":
                    if sub not in ("function_call", "function_call_output", "custom_tool_call"):
                        continue
                    if sub == "function_call_output":
                        _fast_output(line, pending)
                        continue
                elif rec == "event_msg":
                    if sub == "item_completed":
                        if not any(k in kinds for k in _CODEX_ITEM_TYPES) and not any(k in line[:6000] for k in _CODEX_ITEMS):
                            continue
                    elif sub != "exec_command_end":
                        continue
                elif rec not in ("session_meta", "turn_context"):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                typ = entry.get("type")
                p = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                ts = parse_ts(entry.get("timestamp"))
                if typ == "session_meta":
                    session = p.get("id") or p.get("session_id") or session
                    cwd = p.get("cwd") or cwd
                    subagent = bool(p.get("forked_from_id") or p.get("agent_path") or p.get("parent_thread_id"))
                    continue
                if typ == "turn_context":
                    cwd = p.get("cwd") or cwd
                    continue
                ptype = p.get("type")
                if typ == "response_item" and ptype == "function_call":
                    name = p.get("name")
                    try:
                        args = json.loads(p.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    if name in ("exec_command", "shell", "local_shell", "container.exec"):
                        command = _cmd_text(args.get("cmd") or args.get("command"))
                        act = Action(id=p.get("call_id"), agent="codex", session=session, subagent=subagent,
                                     project=args.get("workdir") or cwd, ts=ts, tool="shell", command=command,
                                     steps=tool_steps("shell", command, extra))
                    elif name == "apply_patch":
                        paths = _patch_paths(args.get("input") or args.get("patch") or "")
                        act = Action(id=p.get("call_id"), agent="codex", session=session, subagent=subagent,
                                     project=cwd, ts=ts, tool="apply_patch", target=", ".join(paths[:3]) or None,
                                     steps=["edit"])
                    elif name in ("spawn_agent", "send_message", "followup_task"):
                        act = Action(id=p.get("call_id"), agent="codex", session=session, subagent=subagent,
                                     project=cwd, ts=ts, tool=name, steps=["delegate"])
                    else:
                        continue
                    if _in_window(ts, since, until):
                        old.append(act)
                        if act.id:
                            pending[act.id] = act
                elif typ == "response_item" and ptype == "custom_tool_call" and p.get("name") == "apply_patch":
                    paths = _patch_paths(p.get("input") or "")
                    act = Action(id=p.get("call_id"), agent="codex", session=session, subagent=subagent, project=cwd,
                                 ts=ts, tool="apply_patch", target=", ".join(paths[:3]) or None, steps=["edit"])
                    if _in_window(ts, since, until):
                        old.append(act)
                        if act.id:
                            pending[act.id] = act
                elif typ == "response_item" and ptype == "function_call_output":
                    act = pending.pop(p.get("call_id"), None)
                    out = p.get("output")
                    if act is None or not isinstance(out, str):
                        continue
                    m = EXIT_CODE.search(out)
                    if m:
                        act.exit_code = int(m.group(1))
                        act.outcome = "ok" if act.exit_code == 0 else "failed"
                    elif act.tool != "shell":
                        act.outcome = "ok"
                    w = WALL_TIME.search(out)
                    if w:
                        act.duration = float(w.group(1))
                elif typ == "event_msg" and ptype == "exec_command_end":
                    act = pending.get(p.get("call_id"))
                    if act is not None and isinstance(p.get("exit_code"), int):
                        act.exit_code = p["exit_code"]
                        act.outcome = "ok" if act.exit_code == 0 else "failed"
                elif typ == "event_msg" and ptype == "item_completed":
                    item = p.get("item") if isinstance(p.get("item"), dict) else {}
                    itype = item.get("type")
                    if not _in_window(ts, since, until):
                        continue
                    status = str(item.get("status") or "")
                    outcome = "ok" if status == "completed" else "denied" if status == "declined" else (
                        "failed" if status == "failed" else "unknown")
                    dur = item.get("duration")
                    duration = (dur.get("secs", 0) + dur.get("nanos", 0) / 1e9) if isinstance(dur, dict) else None
                    if itype == "CommandExecution":
                        command = _cmd_text(item.get("command"))
                        ec = item.get("exit_code")
                        if isinstance(ec, int):
                            outcome = "ok" if ec == 0 else "failed"
                        new.append(Action(id=item.get("id"), agent="codex", session=session, subagent=subagent,
                                          project=item.get("cwd") or cwd, ts=ts, tool="shell", command=command,
                                          steps=tool_steps("shell", command, extra), outcome=outcome,
                                          exit_code=ec if isinstance(ec, int) else None, duration=duration))
                    elif itype == "FileChange":
                        changes = item.get("changes")
                        paths = list(changes.keys()) if isinstance(changes, dict) else []
                        new.append(Action(id=item.get("id"), agent="codex", session=session, subagent=subagent,
                                          project=cwd, ts=ts, tool="FileChange", target=", ".join(paths[:3]) or None,
                                          steps=["edit"], outcome=outcome))
                    elif itype == "McpToolCall":
                        target = "%s:%s" % (item.get("server"), item.get("tool"))
                        new.append(Action(id=item.get("id"), agent="codex", session=session, subagent=subagent,
                                          project=cwd, ts=ts, tool="mcp:" + target, target=target, steps=["mcp"],
                                          outcome=outcome, duration=duration))
                    elif itype == "WebSearch":
                        new.append(Action(id=item.get("id"), agent="codex", session=session, subagent=subagent,
                                          project=cwd, ts=ts, tool="WebSearch", steps=["web"], outcome="ok"))
        # Newer sessions record every command as a CommandExecution event; prefer those over the older call log.
        chosen = new if any(a.tool == "shell" for a in new) else old + new
        for act in chosen:
            key = "codex:%s:%s" % (session, act.id) if act.id else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            yield act


def load_actions(agents=("claude-code", "codex"), since=None, until=None, claude_dir=None, codex_dir=None,
                 extra=None, project=None):
    """Load, filter and sort actions from every supported agent."""
    actions = []
    if "claude-code" in agents:
        actions.extend(read_claude(claude_dir, since, until, extra))
    if "codex" in agents:
        actions.extend(read_codex(codex_dir, since, until, extra))
    if project:
        needle = project.lower()
        actions = [a for a in actions if a.project and needle in a.project.lower()]
    actions.sort(key=lambda a: (a.ts or 0))
    for i, a in enumerate(actions):
        a.order = i
    return actions
