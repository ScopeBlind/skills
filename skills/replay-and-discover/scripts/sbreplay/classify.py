"""Turn a tool call into steps: test, build, commit, push, deploy, publish, delete, and so on.

A shell command is split into the commands it actually runs (outside quotes and heredoc bodies), wrappers such as
`zsh -c '...'`, `screen -dmS name ...`, `timeout`, `sudo` and environment assignments are peeled off, and each
command is matched against the step patterns below. A command someone only mentions (inside a commit message, an
echo, or a heredoc) is not a step.
"""
import re

# Step kind -> (label, category). Categories: check, finish, change, risky, other.
STEPS = {
    "test": ("Run tests", "check"),
    "typecheck": ("Type-check", "check"),
    "build": ("Build", "check"),
    "lint": ("Lint or format", "check"),
    "ci_check": ("Check CI", "check"),
    "pr_review": ("Review a pull request", "check"),
    "install": ("Install dependencies", "change"),
    "edit": ("Edit files", "change"),
    "commit": ("Commit", "finish"),
    "push": ("Push", "finish"),
    "pr_open": ("Open a pull request", "finish"),
    "pr_merge": ("Merge a pull request", "finish"),
    "deploy": ("Deploy", "finish"),
    "publish": ("Publish a package or release", "finish"),
    "force_push": ("Force-push", "risky"),
    "git_rewrite": ("Rewrite or discard git history", "risky"),
    "delete": ("Delete files recursively", "risky"),
    "db_change": ("Change a database", "risky"),
    "cloud_change": ("Change cloud resources", "risky"),
    "permissions": ("Change permissions or run as root", "risky"),
    "secret_read": ("Read secrets", "risky"),
    "web_request": ("Call a URL", "other"),
    "run_script": ("Run a project script", "other"),
    "read": ("Read files", "other"),
    "web": ("Browse or search the web", "other"),
    "mcp": ("Use a connected tool", "other"),
    "delegate": ("Hand work to a sub-agent", "other"),
    "other_command": ("Other command", "other"),
}
FINISH = ["commit", "push", "pr_open", "pr_merge", "deploy", "publish"]
CHECKS = ["test", "typecheck", "build", "lint", "ci_check", "pr_review"]
RISKY = ["deploy", "publish", "pr_merge", "force_push", "git_rewrite", "delete", "db_change", "cloud_change",
         "permissions", "secret_read"]
# Steps that shape a workflow. Reads, web lookups and unclassified commands are left out of sequences as noise.
FLOW_STEPS = set(CHECKS + FINISH + ["install", "edit", "force_push", "git_rewrite", "delete", "db_change",
                                    "cloud_change", "web_request"])

_GIT = r"git(?:\s+-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+))*\s+"
_NPM = r"(?:npm|pnpm|yarn|bun)\s+"
_RUN = r"(?:npm|pnpm|yarn|bun)\s+run\s+(?:-s\s+|--silent\s+)?"

_PATTERNS = [
    ("force_push", _GIT + r"push\b.*(?:\s--force(?:-with-lease)?\b|\s-f\b|\s\+\S)"),
    ("git_rewrite", _GIT + r"(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s|restore\s+--source|rebase\b|"
                           r"filter-branch|filter-repo|stash\s+(?:drop|clear)|branch\s+-D\b|push\s+\S+\s+:\S)"),
    ("push", _GIT + r"push\b"),
    ("commit", _GIT + r"commit\b"),
    ("pr_merge", r"gh\s+pr\s+merge\b"),
    ("pr_open", r"gh\s+pr\s+create\b"),
    ("pr_review", r"gh\s+pr\s+review\b"),
    ("ci_check", r"gh\s+(?:pr\s+checks|run\s+(?:watch|view|list))\b|gh\s+pr\s+view\b.*statusCheckRollup"),
    ("publish", _NPM + r"publish\b|cargo\s+publish\b|twine\s+upload\b|gem\s+push\b|poetry\s+publish\b|"
                r"uv\s+publish\b|gh\s+release\s+create\b|docker\s+push\b|dotnet\s+nuget\s+push\b"),
    ("db_change", r"prisma\s+(?:migrate\s+deploy|db\s+push)\b|(?:rails|rake)\s+db:migrate\b|alembic\s+upgrade\b|"
                  r"flyway\s+migrate\b|knex\s+migrate:latest\b|sequelize\s+db:migrate\b|"
                  r"wrangler\s+d1\s+(?:migrations\s+apply|execute)\b.*--remote|"
                  r"(?:psql|mysql|sqlite3)\b.*\b(?:drop|delete\s+from|truncate|alter\s+table|update\s+\w+\s+set)\b"),
    ("deploy", r"wrangler\s+(?:pages\s+deploy|deploy|versions\s+deploy|publish)\b|vercel\b(?:.*\s--prod\b|\s+deploy\b)|"
               r"netlify\s+deploy\b|fly(?:ctl)?\s+deploy\b|firebase\s+deploy\b|gcloud\s+(?:app|run|functions)\s+deploy\b|"
               r"heroku\s+(?:container:release|releases:rollback)\b|cdk\s+deploy\b|(?:serverless|sls)\s+deploy\b|"
               r"terraform\s+apply\b|pulumi\s+up\b|kubectl\s+(?:apply|rollout\s+restart|set\s+image)\b|"
               r"helm\s+(?:upgrade|install)\b|eas\s+(?:update|submit)\b|railway\s+up\b|" + _RUN + r"deploy\b|"
               r"(?:(?:node|bash|sh|zsh|python3?)\s+)?\S*deploy[\w.-]*\.(?:sh|mjs|cjs|js|ts|py)\b"),
    ("cloud_change", r"aws\s+\S+\s+(?:create|delete|put|update|terminate|run-instances|rm|sync|deploy)[\w-]*\b|"
                     r"gcloud\s+\S+(?:\s+\S+)?\s+(?:create|delete|update|deploy)\b|az\s+\S+(?:\s+\S+)?\s+(?:create|delete|update)\b|"
                     r"wrangler\s+(?:secret\s+(?:put|delete)|kv\s+key\s+(?:put|delete)|r2\s+object\s+(?:put|delete)|"
                     r"d1\s+delete|pages\s+(?:project\s+delete|deployment\s+delete))\b|kubectl\s+delete\b"),
    ("delete", r"rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b|find\b.*\s-delete\b|shred\b"),
    ("permissions", r"chmod\s+(?:-R\s+)?(?:777|a\+rwx|o\+w)\b|chown\s+-R\b"),
    ("secret_read", r"cat\s+\S*\.env\b|security\s+find-(?:generic|internet)-password\b|op\s+(?:read|item\s+get)\b|"
                    r"gcloud\s+secrets\s+versions\s+access\b|aws\s+secretsmanager\s+get-secret-value\b|"
                    r"vault\s+(?:kv\s+get|read)\b|printenv\b"),
    ("test", r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:-s\s+|--silent\s+)?test(?::\S+)?\b|pytest\b|"
             r"python3?\s+-m\s+(?:pytest|unittest)\b|go\s+test\b|cargo\s+test\b|vitest\b|jest\b|mocha\b|"
             r"node\s+(?:--import\s+\S+\s+)?--test\b|playwright\s+test\b|deno\s+test\b|bun\s+test\b|"
             r"mvn\s+(?:-\S+\s+)*test\b|(?:\./)?gradlew?\s+test\b|rspec\b|phpunit\b|make\s+test\b|tox\b|"
             r"swift\s+test\b|dotnet\s+test\b|xcodebuild\b.*\btest\b|" + _RUN + r"(?:test|e2e|check)[\w:-]*\b"),
    ("typecheck", r"tsc\b.*--noEmit\b|" + _RUN + r"(?:typecheck|type-check|tsc)[\w:-]*\b|mypy\b|pyright\b"),
    ("build", _RUN + r"build[\w:-]*\b|tsc\b|cargo\s+build\b|go\s+build\b|make\b|gradle\s+build\b|"
              r"mvn\s+(?:package|install)\b|vite\s+build\b|next\s+build\b|webpack\b|xcodebuild\b|swift\s+build\b|"
              r"dotnet\s+build\b"),
    ("lint", r"eslint\b|prettier\b|ruff\b|black\b|flake8\b|pylint\b|golangci-lint\b|rubocop\b|"
             + _RUN + r"(?:lint|format|fmt)[\w:-]*\b|cargo\s+(?:clippy|fmt)\b|gofmt\b|swiftlint\b"),
    ("install", _NPM + r"(?:install|i|ci|add)\b|pip3?\s+install\b|uv\s+(?:pip\s+install|add|sync)\b|"
                r"poetry\s+(?:add|install)\b|brew\s+install\b|cargo\s+(?:add|install)\b|go\s+get\b|"
                r"gem\s+install\b|bundle\s+install\b|apt(?:-get)?\s+install\b"),
    ("web_request", r"(?:curl|wget|http|https|xh)\b.*\bhttps?://"),
    ("run_script", _RUN + r"\S+|make\b|just\b|\./\S+|python3?\s+\S+\.py\b|node\s+\S+\.[mc]?js\b|(?:ba|z)?sh\s+\S+\.sh\b"),
]
_COMPILED = [(kind, re.compile(r"^(?:" + pat + r")", re.I)) for kind, pat in _PATTERNS]

_PREFIX = re.compile(
    r"^(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)|timeout\s+\S+\s+|"
    r"perl\s+-e\s+'[^']*'\s+|caffeinate(?:\s+-\w+)*\s+|nohup\s+|time\s+|command\s+|exec\s+|"
    r"sudo\s+(?:-\S+\s+)*|env\s+(?:-\S+\s+)*|(?:npx|bunx|pnpx)\s+(?:-y\s+|--yes\s+)?|pnpm\s+(?:dlx|exec)\s+|"
    r"yarn\s+dlx\s+|uvx\s+|pipx\s+run\s+|[({!]\s*)+"
)
_SHELL_WRAP = re.compile(
    r"^(?:screen\s+(?:-\S+\s+)*(?:[\w.-]+\s+)?)?(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\s+(['\"])([\s\S]*)\1"
    r"(?:\s*(?:\d*>>?|&>)\s*\S+)*\s*&?\s*$"
)
_DRY_RUN = re.compile(r"(?:^|\s)(?:--dry-run|--dryrun|--dry_run|--noop|--what-if)\b|^git\s+push\b.*\s-n\b")
_REAL_EFFECT = {"publish", "deploy", "push", "force_push", "pr_merge", "db_change", "cloud_change", "delete", "git_rewrite"}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SUDO = re.compile(r"^\s*sudo\b")


def strip_heredocs(command):
    """Drop heredoc bodies, which are data rather than commands."""
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC.search(line)
        i += 1
        if m:
            term = m.group(2)
            while i < len(lines) and lines[i].strip() != term:
                i += 1
            i += 1
    return "\n".join(out)


def split_commands(command):
    """Split on ; && || | & and newlines outside quotes. Returns the raw command segments."""
    segs, cur, i, n = [], [], 0, len(command)
    quote = None
    while i < n:
        ch = command[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                cur.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            cur.append(ch)
            cur.append(command[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            i += 1
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            segs.append("".join(cur))
            cur = []
            i += 2
            continue
        if ch in (";", "|", "\n") or (ch == "&" and not (i > 0 and command[i - 1] in "<>") and command[i + 1:i + 2] != ">"):
            segs.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def _peel(segment):
    prev = None
    s = segment.strip()
    while prev != s:
        prev = s
        s = _PREFIX.sub("", s).strip()
    return s


def command_steps(command, extra=None, depth=0):
    """Return the ordered list of step kinds a shell command performs (possibly empty)."""
    if not isinstance(command, str) or not command.strip():
        return []
    steps = []
    for seg in split_commands(strip_heredocs(command)):
        if _SUDO.match(seg):
            steps.append("permissions")
        s = _peel(seg)
        wrapped = _SHELL_WRAP.match(s)
        if wrapped and depth < 3:
            steps.extend(command_steps(wrapped.group(2), extra, depth + 1))
            continue
        if not s or s.startswith("cd ") or s == "cd":
            continue
        kind = None
        if extra:
            for k, rx in extra:
                if rx.search(s):
                    kind = k
                    break
        if kind is None:
            for k, rx in _COMPILED:
                if rx.match(s):
                    kind = k
                    break
        if kind in _REAL_EFFECT and _DRY_RUN.search(s):
            kind = "other_command"
        steps.append(kind or "other_command")
    return steps


def focus(command, steps, extra=None, depth=0):
    """The part of a shell command that performed one of `steps` (its raw text), or None."""
    wanted = set(steps or [])
    for seg in split_commands(strip_heredocs(command or "")):
        s = _peel(seg)
        wrapped = _SHELL_WRAP.match(s)
        if wrapped and depth < 3:
            inner = focus(wrapped.group(2), steps, extra, depth + 1)
            if inner:
                return inner
            continue
        if wanted & set(command_steps(seg, extra, depth=3)):
            return seg.strip()
    return None


_SETUP = re.compile(r"^(?:export|set|source|\.|unset|trap|local|declare|readonly|shopt|ulimit|umask|cd|pushd|popd)\b|"
                    r"^[A-Za-z_][A-Za-z0-9_]*=|^(?:python3?|node|ruby|perl|bash|sh|zsh)\s+(?:-\s*$|-\s*<<|-c\b|-e\b|-\s)")


def meaningful_segments(command):
    """Segments that do something, with setup (export, set, cd, assignments) and one-off inline scripts removed."""
    out = []
    for seg in split_commands(strip_heredocs(command or "")):
        s = _peel(seg)
        if s and not _SETUP.match(s):
            out.append(s)
    return out


def compile_extra(steps_map):
    """Compile custom step patterns from a rules file: {"live_check": ["curl .*example.com"]}."""
    out = []
    for kind, pats in (steps_map or {}).items():
        for p in pats if isinstance(pats, list) else [pats]:
            try:
                out.append((kind, re.compile(p, re.I)))
            except re.error:
                continue
    return out


_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "FileChange"}
_READ_TOOLS = {"Read", "Glob", "Grep", "LS", "view_image", "NotebookRead"}
_WEB_TOOLS = {"WebFetch", "WebSearch", "web__run", "web_search", "WebSearchCall"}
_DELEGATE_TOOLS = {"Task", "Agent", "spawn_agent", "send_message", "followup_task", "Workflow"}


def tool_steps(tool, command=None, extra=None):
    """Steps for any tool call: shell commands are parsed; other tools map to one step."""
    if tool in ("Bash", "shell", "exec_command", "local_shell", "CommandExecution"):
        return command_steps(command or "", extra) or ["other_command"]
    if tool in _EDIT_TOOLS:
        return ["edit"]
    if tool in _READ_TOOLS:
        return ["read"]
    if tool in _WEB_TOOLS:
        return ["web"]
    if tool in _DELEGATE_TOOLS:
        return ["delegate"]
    if isinstance(tool, str) and (tool.startswith("mcp__") or tool.startswith("mcp:")):
        return ["mcp"]
    return []


GERUND = {
    "commit": "committing", "push": "pushing", "pr_open": "opening a pull request", "pr_merge": "merging a pull request",
    "deploy": "deploying", "publish": "publishing", "force_push": "force-pushing", "git_rewrite": "rewriting git history",
    "delete": "deleting files recursively", "db_change": "changing a database", "cloud_change": "changing cloud resources",
    "permissions": "changing permissions", "secret_read": "reading secrets", "test": "running tests",
}


NOUN = {"test": "tests", "typecheck": "a type-check", "build": "a build", "lint": "lint", "ci_check": "a CI check",
        "pr_review": "a review", "web_request": "a URL check", "edit": "a file edit"}


def noun(kind):
    """'tests', 'a CI check': for sentences such as 'tests did not run'."""
    return NOUN.get(kind, lc(label(kind)))


def gerund(kind):
    """'deploying', 'opening a pull request': for sentences such as 'before deploying'."""
    return GERUND.get(kind, lc(label(kind)))


def lc(text):
    """Lower-case the first letter only, so 'Check CI' becomes 'check CI'."""
    return text[:1].lower() + text[1:] if text else text


def label(kind):
    return STEPS.get(kind, (kind.replace("_", " ").capitalize(), "other"))[0]


def category(kind):
    return STEPS.get(kind, ("", "other"))[1]
