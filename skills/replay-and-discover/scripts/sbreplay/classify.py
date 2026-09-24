"""Turn a tool call into steps: test, build, commit, push, deploy, publish, delete, and so on.

A shell command is split into the commands it actually runs (outside quotes and heredoc bodies), together with the
operator that joins each one to the next: &&, ||, ;, |, & or a line break. Wrappers such as `zsh -c '...'`,
`screen -dmS name ...`, `timeout`, `sudo`, `if`/`then` and environment assignments are peeled off, and each command
is matched against the step patterns below. A command someone only mentions (inside a commit message, an echo or a
heredoc) is not a step.

The joins matter. In `npm test && git push` the push runs only if the tests passed; in `npm test; git push` and in
`npm test | tail -5 && git push` it runs either way. `gated()` answers that question for two parts of a command, and
`writes()` says whether a part changes files under a given folder.
"""
import os
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
    "git_update": ("Update files from git", "change"),
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
    "inline_script": ("Run an inline script", "other"),
    "inspect": ("Look at files or state", "other"),
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
FLOW_STEPS = set(CHECKS + FINISH + ["install", "edit", "git_update", "force_push", "git_rewrite", "delete",
                                    "db_change", "cloud_change", "web_request"])
# Steps that can carry the name of a project's own tool and so are worth labelling when they recur.
UNPLACED = ("run_script", "other_command")

_GIT = r"git(?:\s+-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+))*\s+"
_NPM = r"(?:npm|pnpm|yarn|bun)\s+"
_RUN = r"(?:npm|pnpm|yarn|bun)\s+run\s+(?:-s\s+|--silent\s+)?"

_PATTERNS = [
    ("force_push", _GIT + r"push\b.*(?:\s--force(?:-with-lease)?\b|\s-f\b|\s\+\S)"),
    ("git_rewrite", _GIT + r"(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s|restore\s+--source|rebase\b|"
                           r"filter-branch|filter-repo|stash\s+(?:drop|clear)|branch\s+-D\b|push\s+\S+\s+:\S)"),
    ("push", _GIT + r"push\b"),
    ("commit", _GIT + r"commit\b"),
    ("git_update", _GIT + r"(?:pull|am|apply|cherry-pick|revert)\b|" + _GIT + r"merge(?![\w-])|" +
                   _GIT + r"stash(?!\s+(?:list|show|drop|clear|branch|create)\b)(?![\w-])|" +
                   _GIT + r"checkout\s+(?!-[bB]\b|--\s)|" + _GIT + r"switch\s+(?!-[cC]\b|--create\b)|" +
                   _GIT + r"restore\b"),
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
               r"gh\s+workflow\s+run\b.*\b(?:deploy|release|publish)|"
               r"(?:(?:node|bash|sh|zsh|python3?)\s+)?\S*deploy[\w.-]*\.(?:sh|mjs|cjs|js|ts|py)\b"),
    ("cloud_change", r"aws\s+\S+\s+(?:create|delete|put|update|terminate|run-instances|rm|sync|deploy)[\w-]*\b|"
                     r"gcloud\s+\S+(?:\s+\S+)?\s+(?:create|delete|update|deploy)\b|az\s+\S+(?:\s+\S+)?\s+(?:create|delete|update)\b|"
                     r"wrangler\s+(?:secret\s+(?:put|delete)|kv\s+key\s+(?:put|delete)|r2\s+object\s+(?:put|delete)|"
                     r"d1\s+delete|pages\s+(?:project\s+delete|deployment\s+delete))\b|kubectl\s+delete\b"),
    ("delete", r"rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b|find\b.*\s(?:-delete\b|-exec\s+rm\b)|shred\b"),
    ("permissions", r"chmod\s+(?:-R\s+)?(?:777|a\+rwx|o\+w)\b|chown\s+-R\b"),
    ("secret_read", r"cat\s+\S*\.env\b|(?:source|\.)\s+\S*\.env\b|security\s+find-(?:generic|internet)-password\b|"
                    r"op\s+(?:read|item\s+get)\b|gcloud\s+secrets\s+versions\s+access\b|"
                    r"aws\s+secretsmanager\s+get-secret-value\b|vault\s+(?:kv\s+get|read)\b|printenv\b"),
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
             + _RUN + r"(?:lint|format|fmt)[\w:-]*\b|cargo\s+(?:clippy|fmt)\b|gofmt\b|swiftlint\b|swiftformat\b"),
    ("install", _NPM + r"(?:install|i|ci|add)\b|pip3?\s+install\b|uv\s+(?:pip\s+install|add|sync)\b|"
                r"poetry\s+(?:add|install)\b|brew\s+install\b|cargo\s+(?:add|install)\b|go\s+get\b|"
                r"gem\s+install\b|bundle\s+install\b|apt(?:-get)?\s+install\b"),
    ("edit", r"(?:npm|pnpm|yarn)\s+(?:version|pkg\s+set)\b|patch\b"),
    ("web_request", r"(?:curl|wget|http|https|xh)\b.*\bhttps?://"),
    ("inline_script", r"(?:python3?|node|ruby|perl|deno|bun|osascript|bash|sh|zsh)\s+(?:-\s*$|-\s*<<|-c\b|-e\b|-\s|"
                      r"--eval\b|--input-type=\S+\s+(?:-e|--eval)\b|eval\b)"),
]
_INSPECT = (r"(?:ls|ll|cat|bat|head|tail|less|more|wc|stat|file|tree|du|df|pwd|whoami|which|type|hostname|uname|date|"
            r"uptime|ps|lsof|id|echo|printf|true|false|sleep|test|\[\[?|basename|dirname|realpath|readlink|md5sum|md5|"
            r"shasum|sha256sum|cksum|diff|cmp|comm|sort|uniq|cut|tr|column|nl|jq|yq|xxd|hexdump|od|strings|grep|egrep|"
            r"fgrep|rg|ag|fd|find|mdfind|awk|sed|open|wait|read|history|man|seq|expr|bc|nproc|sysctl|sw_vers|"
            r"defaults\s+read|launchctl\s+(?:list|print)|security\s+find-certificate|codesign\s+-[dv]|"
            r"lsregister|mdls|plutil\s+-p|pgrep|curl\s+(?:-\S+\s+)*-I)\b|"
            + _GIT + r"(?:status|diff|log|show|branch(?!\s+-[dDmM])|rev-parse|rev-list|ls-files|ls-tree|ls-remote|"
            r"remote(?:\s+-v|\s+show|\s+get-url|\s*$)|describe|blame|reflog|shortlog|config\s+--get|worktree\s+list|"
            r"stash\s+(?:list|show)|tag\s*$|tag\s+-l|cat-file|merge-base|for-each-ref|grep|fetch|check-ignore|"
            r"name-rev|count-objects|verify-commit|verify-tag|var|help)\b|"
            r"gh\s+(?:pr|issue|run|release|repo|workflow|label|secret|variable)\s+(?:view|list|status|diff|download)\b|"
            r"gh\s+(?:auth\s+status|api\b(?!.*\s(?:-X|--method)\s*(?:POST|PUT|PATCH|DELETE))|search\b|browse\b)|"
            r"(?:node|python3?|ruby|go|cargo|npm|npx|pnpm|yarn|deno|bun|rustc|java|swift|git|gh|wrangler|docker|"
            r"kubectl|terraform|codex|claude|brew|pip3?|uv)\s+(?:--version|-v|-V|version|--help|-h|help)\b|"
            r"(?:npm|pnpm|yarn)\s+(?:ls|list|view|info|outdated|audit|whoami|config\s+(?:get|list)|pack\s+--dry-run|"
            r"explain|why|search)\b|wrangler\s+(?:\S+\s+)*(?:list|tail|whoami|info)\b|docker\s+(?:ps|images|logs|"
            r"inspect)\b|kubectl\s+(?:get|describe|logs)\b|brew\s+(?:list|info|search|outdated|doctor)\b|"
            r"pip3?\s+(?:list|show|freeze)\b|launchctl\s+(?:list|print)\b")
_RUN_SCRIPT = (_RUN + r"\S+|make\b|just\b|\./\S+|python3?\s+\S+\.py\b|node\s+\S+\.[mc]?[jt]s\b|"
               r"(?:tsx|ts-node|deno\s+run|bun(?:\s+run)?)\s+\S+\.[mc]?[jt]s\b|(?:ba|z)?sh\s+\S+\.sh\b|\S+\.sh\b")
_COMPILED = [(kind, re.compile(r"^(?:" + pat + r")", re.I)) for kind, pat in _PATTERNS]
_INSPECT_RX = re.compile(r"^(?:" + _INSPECT + r")", re.I)
_RUN_SCRIPT_RX = re.compile(r"^(?:" + _RUN_SCRIPT + r")", re.I)

_PREFIX = re.compile(
    r"^(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s\"'][^\s]*|)\s+)|timeout\s+\S+\s+|"
    r"gtimeout\s+\S+\s+|perl\s+-e\s+'[^']*'\s+|caffeinate(?:\s+-\w+)*\s+|nohup\s+|setsid\s+|time\s+|command\s+|"
    r"exec\s+|builtin\s+|sudo\s+(?:-\S+\s+)*|env\s+(?:-\S+\s+)*|(?:npx|bunx|pnpx)\s+(?:-y\s+|--yes\s+)?|"
    r"pnpm\s+(?:dlx|exec)\s+|yarn\s+dlx\s+|uvx\s+|pipx\s+run\s+|xargs\s+(?:-\S+\s+)*|"
    r"(?:if|then|else|elif|do|while|until)\s+|[({!]\s*)+"
)
_SHELL_WRAP = re.compile(
    r"^(?:screen\s+(?:-\S+\s+)*(?:[\w.-]+\s+)?)?(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\s+(['\"])([\s\S]*)\1"
    r"(?:\s*(?:\d*>>?|&>)\s*\S+)*\s*&?\s*$"
)
_DETACHED = re.compile(r"^screen\s+(?:-\S+\s+)*?-\w*d")
_BACKGROUND = re.compile(r"(?:^|\s)(?:nohup|setsid|disown)\b")
_DRY_RUN = re.compile(r"(?:^|\s)(?:--dry-run|--dryrun|--dry_run|--noop|--what-if)\b|^git\s+push\b.*\s-n\b")
_REAL_EFFECT = {"publish", "deploy", "push", "force_push", "pr_merge", "db_change", "cloud_change", "delete", "git_rewrite"}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SUDO = re.compile(r"^\s*sudo\b")
_NOSTEP = re.compile(r"^(?:cd|pushd|popd|export|unset|set|shopt|source|\.|local|declare|typeset|readonly|trap|alias|"
                     r"ulimit|umask|hash|shift|return|exit|break|continue|fi|done|esac|else|then|do|in|function)(?:\s|$)|"
                     r"^[A-Za-z_][A-Za-z0-9_]*=\S*$|^[A-Za-z_][A-Za-z0-9_]*=[\"'][\s\S]*[\"']$|^[{}()]+$|^;;$|"
                     r"^\w+\s*\(\s*\)\s*\{?$")
_SETUP = re.compile(r"^(?:export|set|source|\.|unset|trap|local|declare|readonly|shopt|ulimit|umask|cd|pushd|popd)\b|"
                    r"^[A-Za-z_][A-Za-z0-9_]*=|^(?:python3?|node|ruby|perl|bash|sh|zsh)\s+(?:-\s*$|-\s*<<|-c\b|-e\b|-\s)")
_HOME = os.path.expanduser("~")


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


_TOKENS = re.compile(r"""'[^']*'?|"(?:[^"\\]|\\[\s\S])*"?|\\[\s\S]|&&|\|\||\|&|[;|&\n]|[^'"\\;&|\n]+|\\""")


def split_with_joiners(command):
    """Split on ; && || | & and line breaks outside quotes: [(segment, joiner after it)], the last joiner None."""
    segs, cur = [], []

    def cut(joiner):
        text = "".join(cur).strip()
        if text:
            segs.append([text, joiner])
        del cur[:]

    tokens = _TOKENS.findall(command)
    for i, tok in enumerate(tokens):
        if tok in ("&&", "||", "|&", ";", "|", "\n") or (tok == "&" and not (
                cur and cur[-1][-1:] in "<>") and not tokens[i + 1:i + 2] == [">"] and not (
                i + 1 < len(tokens) and tokens[i + 1].startswith(">"))):
            if tok == "\n" and not "".join(cur).strip() and segs and segs[-1][1] in ("&&", "||", "|"):
                continue  # a line break after && || | continues the same list
            cut("|" if tok == "|&" else tok)
            continue
        cur.append(tok)
    cut(None)
    if segs:
        segs[-1][1] = None if segs[-1][1] in (";", "\n", None) else segs[-1][1]
    return [(t, j) for t, j in segs]


def split_commands(command):
    """Split on ; && || | & and newlines outside quotes. Returns the raw command segments."""
    return [t for t, _ in split_with_joiners(command)]


def _peel(segment):
    prev = None
    s = segment.strip()
    while prev != s:
        prev = s
        s = _PREFIX.sub("", s).strip()
    return s


class Seg(object):
    """One command inside a shell command line, with how it is joined to its neighbours."""
    __slots__ = ("raw", "core", "steps", "before", "after", "group", "block", "errexit", "pipefail", "background",
                 "negated", "dir", "env")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):  # pragma: no cover
        return "Seg(%r %s %r)" % (self.core, self.steps, self.after)


def _segment_kinds(raw, extra):
    kinds = []
    if _SUDO.match(raw):
        kinds.append("permissions")
    s = _peel(raw)
    if not s or _NOSTEP.match(s):
        return kinds
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
    if kind is None and _WRITER_HEAD.match(s) and _is_writer(s):
        kind = "edit"
    if kind is None and _INSPECT_RX.match(s):
        kind = "inspect"
    if kind is None and _RUN_SCRIPT_RX.match(s):
        kind = "run_script"
    if kind in _REAL_EFFECT and _DRY_RUN.search(s):
        kind = "other_command"
    kinds.append(kind or "other_command")
    return kinds


def _block_marks(t):
    """How many blocks (subshells, groups, if/for/while/case) a segment opens and closes."""
    opens_paren = opens_other = closes_paren = closes_other = 0
    i = 0
    while i < len(t) and t[i] in "({! \t":
        if t[i] == "(":
            opens_paren += 1
        elif t[i] == "{":
            opens_other += 1
        i += 1
    rest = t[i:]
    if re.match(r"(?:if|for|while|until|case|select)\b", rest) or re.match(r"(?:function\s+\w+|\w+\s*\(\s*\))", rest):
        opens_other += 1
    if re.match(r"(?:fi|done|esac)\b", rest):
        closes_other += 1
    if "$(" not in t and "((" not in t:
        j = len(t)
        while j > 0 and t[j - 1] in ")} \t":
            if t[j - 1] == ")":
                closes_paren += 1
            elif t[j - 1] == "}":
                closes_other += 1
            j -= 1
    return opens_paren, opens_other, closes_paren, closes_other


def expand_word(raw, env=None, cur=None):
    """Expand one shell word the simple way (quotes, ~, $VAR, ${VAR}); None if it needs anything more."""
    if raw is None or "$(" in raw or "`" in raw or "<(" in raw:
        return None
    env = env or {}
    out, i, n, dq = [], 0, len(raw), False
    while i < n:
        ch = raw[i]
        if ch == "'" and not dq:
            j = raw.find("'", i + 1)
            if j < 0:
                return None
            out.append(raw[i + 1:j])
            i = j + 1
            continue
        if ch == '"':
            dq = not dq
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == "$":
            m = re.match(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))", raw[i:])
            if not m:
                return None
            name = m.group(1) or m.group(2)
            if name in env:
                val = env[name]
            elif name == "HOME":
                val = _HOME
            elif name == "PWD":
                val = cur
            elif name == "TMPDIR":
                val = os.environ.get("TMPDIR") or "/tmp"
            else:
                val = None
            if val is None:
                return None
            out.append(val)
            i += m.end()
            continue
        if ch == "~" and i == 0 and not dq and (n == 1 or raw[1] == "/"):
            out.append(_HOME)
            i += 1
            continue
        if ch in "*?[" and not dq:
            return None
        out.append(ch)
        i += 1
    return "".join(out)


def resolve_path(raw, env=None, cur=None):
    """An absolute, normalised path for a shell word, or None when it cannot be known without running the shell."""
    word = expand_word(raw, env, cur)
    if word is None or word == "":
        return None
    if not os.path.isabs(word):
        if not cur:
            return None
        word = os.path.join(cur, word)
    return os.path.normpath(word)


def shell_words(s):
    """Split a command into raw words (quotes kept) and redirection operators (returned as ('>', None) pairs)."""
    words, cur, i, n = [], [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch in " \t\n":
            if cur:
                words.append(("".join(cur), True))
                cur = []
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            cur.append(s[i:i + 2])
            i += 2
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and s[j] != ch:
                j += 2 if (ch == '"' and s[j] == "\\") else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if ch in "<>":
            op, k = ch, i + 1
            while k < n and s[k] in "<>&|":
                op += s[k]
                k += 1
            prefix = "".join(cur)
            if cur and (prefix.isdigit() or prefix == "&"):
                op = prefix + op
            elif cur:
                words.append((prefix, True))
            cur = []
            words.append((op, None))
            i = k
            continue
        cur.append(ch)
        i += 1
    if cur:
        words.append(("".join(cur), True))
    return words


def _unq(raw):
    if raw is None:
        return ""
    if not any(c in raw for c in "'\"\\~$"):
        return raw
    return expand_word(raw, {}) if "$" not in raw else raw.strip("\"'")


_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_DIR_FLAGS = re.compile(r"^(?:git\s+-C|(?:npm|pnpm|yarn|make)\b.*?\s(?:--prefix|-C|--dir|--cwd))(?:=|\s+)"
                        r"(\"[^\"]*\"|'[^']*'|\S+)")


_SETUP_HEAD = re.compile(r"^(?:cd|pushd|popd|export|local|declare|readonly|typeset)\b|^[A-Za-z_][A-Za-z0-9_]*=")


def _apply_setup(s, ctx):
    """cd, pushd, popd and plain assignments change what later parts of the command see."""
    if not _SETUP_HEAD.match(s):
        return
    words = [w for w, _ in shell_words(s)]
    if not words:
        return
    head = words[0]
    if head in ("cd", "pushd"):
        target = words[1] if len(words) > 1 and not words[1].startswith("-") else ("~" if len(words) == 1 else None)
        if head == "pushd":
            ctx["stack"].append(ctx["dir"])
        ctx["dir"] = resolve_path(target, ctx["env"], ctx["dir"]) if target else None
        return
    if head == "popd":
        ctx["dir"] = ctx["stack"].pop() if ctx["stack"] else None
        return
    if head in ("export", "local", "declare", "readonly", "typeset"):
        words = [w for w in words[1:] if not w.startswith("-")]
    if words and all(_ASSIGN.match(w) for w in words):
        for w in words:
            m = _ASSIGN.match(w)
            ctx["env"][m.group(1)] = expand_word(m.group(2), ctx["env"], ctx["dir"])


def _segment_dir(s, ctx):
    m = _DIR_FLAGS.match(s)
    if m:
        return resolve_path(m.group(1), ctx["env"], ctx["dir"])
    return ctx["dir"]


def _set_options(s, state):
    words = s.split()[1:]
    i = 0
    while i < len(words):
        t = words[i]
        if t in ("-o", "+o") and i + 1 < len(words):
            name, on = words[i + 1], t == "-o"
            if name in ("errexit", "pipefail"):
                state[name] = on
            i += 2
            continue
        if re.match(r"^[-+][a-zA-Z]+$", t):
            on = t[0] == "-"
            if "e" in t[1:]:
                state["errexit"] = on
            if t.endswith("o") and i + 1 < len(words) and words[i + 1] in ("errexit", "pipefail"):
                state[words[i + 1]] = on
                i += 2
                continue
        i += 1


def plan(command, extra=None, cwd=None):
    """The parts of a shell command in the order they run, each with its steps, joins, options and directory."""
    segs = []
    ctx = {"dir": os.path.normpath(cwd) if cwd else None, "env": {}, "stack": []}
    _plan_into(segs, command if isinstance(command, str) else "", extra, 0, [0], False, ctx, 0)
    return segs


def _plan_into(segs, text, extra, depth, counter, background, ctx, group):
    state = {"errexit": False, "pipefail": False}
    block, paren_dirs, before = 0, [], None
    for raw, joiner in split_with_joiners(strip_heredocs(text)):
        o_paren, o_other, c_paren, c_other = _block_marks(raw)
        for _ in range(o_paren):
            paren_dirs.append((ctx["dir"], dict(ctx["env"])))
        block += o_paren + o_other
        s = _peel(raw)
        bg = background or joiner == "&" or bool(_BACKGROUND.search(raw))
        wrapped = _SHELL_WRAP.match(s)
        if wrapped and depth < 3:
            counter[0] += 1
            inner = {"dir": ctx["dir"], "env": dict(ctx["env"]), "stack": list(ctx["stack"])}
            _plan_into(segs, wrapped.group(2), extra, depth + 1, counter, bg or bool(_DETACHED.match(s)), inner, counter[0])
        else:
            seg = Seg(raw=raw, core=s, steps=_segment_kinds(raw, extra), before=before, after=joiner, group=group,
                      block=block, errexit=state["errexit"], pipefail=state["pipefail"], background=bg,
                      negated=raw.lstrip("( \t{").startswith("!"), dir=_segment_dir(s, ctx), env=dict(ctx["env"]))
            segs.append(seg)
            if s.startswith("set ") and block == 0 and before in (None, ";", "\n") and not bg:
                _set_options(s, state)
            _apply_setup(s, ctx)
        for _ in range(c_paren):
            if paren_dirs:
                ctx["dir"], ctx["env"] = paren_dirs.pop()
        block = max(0, block - c_paren - c_other)
        before = joiner


def gated(segs, r, m=None):
    """True when part `m` of a command runs only if part `r` ran and exited 0; with m=None, when the whole command
    exiting 0 means part `r` exited 0.

    Counted: an unbroken chain of && from r to m, and `set -e` (with `set -o pipefail` for a piped r). Not counted,
    because r's failure would not stop m: ;, ||, a pipe without pipefail, a background job, a negation, anything
    inside a loop or condition, and a different nested shell."""
    n = len(segs)
    if not 0 <= r < n:
        return False
    R = segs[r]
    if R.background or R.negated:
        return False
    if m is not None and (not r < m < n or segs[m].group != R.group or segs[m].background):
        return False
    s = r
    while s > 0 and segs[s].before == "|" and segs[s - 1].group == R.group:
        s -= 1
    p = r
    while p + 1 < n and segs[p].after == "|" and segs[p + 1].group == R.group:
        p += 1
    if p != r and not R.pipefail:
        return False
    if segs[s].before == "||":
        return False
    errexit = (R.errexit and R.block == 0 and segs[s].before in (None, ";", "\n") and segs[p].after in (";", "\n", None))
    if m is None:
        if R.group != 0:
            return False
        return all(segs[k].after == "&&" for k in range(p, n - 1)) or errexit
    if p >= m:
        return False
    return all(segs[k].after == "&&" for k in range(p, m)) or (errexit and bool(segs[m].errexit))


_TEMP = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/dev")
_GENERATED = re.compile(r"(?:^|/)(?:node_modules|dist|build|out|\.next|coverage|target|__pycache__|\.cache|\.turbo|"
                        r"\.pytest_cache|\.venv|venv|\.git)(?:/|$)")
_WRITE_API = re.compile(r"\.write_text\(|\.write_bytes\(|\bopen\([^)\n]*[\"'][wax]b?\+?[\"']|writeFileSync\(|"
                        r"\bfs\.(?:promises\.)?(?:writeFile|appendFile|rm|unlink|rename|copyFile)\w*\(|"
                        r"\bshutil\.(?:copy\w*|move|rmtree)\(|\bos\.(?:remove|unlink|rename|replace)\(|\.unlink\(|"
                        r"\bjson\.dump\(|File\.write|IO\.write")
_PATH_LITERAL = re.compile(r"[\"'](/[^\"'\s]+|~/[^\"'\s]+)[\"']")
_GIT_NO_FILES = re.compile(r"\bgit\b.*\s(?:branch\s+-D\b|stash\s+(?:drop|clear)\b|push\s+\S+\s+:\S)")
_FORMATTER = re.compile(r"^(?:prettier\b.*\s--write\b|eslint\b.*\s--fix\b|ruff\s+(?:format\b|check\b.*--fix\b)|"
                        r"black\b(?!.*--check)|gofmt\s+-\w*w|cargo\s+fmt\b(?!.*--check)|swiftformat\b|"
                        r"(?:npm|pnpm|yarn|bun)\s+run\s+(?:-s\s+)?(?:format|fmt|lint:fix|fix)\b)")
_WRITER_HEAD = re.compile(r"^(?:\S*/)?(?:sed|perl|tee|cp|mv|rsync|install|ln|touch|truncate|rm|patch|unzip|tar|chmod|"
                          r"cat|echo|printf|jq|awk|sort|head|tail|cut|tr|base64|yq)\b")
_WRITE_KINDS = {"edit", "git_update", "git_rewrite", "install", "inline_script", "lint", "delete"}
_WRITER_COMMANDS = {"sed", "perl", "tee", "cp", "mv", "rsync", "install", "ln", "touch", "rm", "truncate", "patch",
                    "unzip", "tar", "chmod"}


_REAL = {}


def real_path(path):
    """A path with symbolic links resolved, as far as it exists on disk (cached), so two spellings of the same folder
    compare equal."""
    if not path:
        return path
    if path in _REAL:
        return _REAL[path]
    head, tail = path, []
    while head and head != os.path.dirname(head) and not os.path.exists(head):
        head, part = os.path.split(head)
        tail.append(part)
    out = os.path.join(os.path.realpath(head), *reversed(tail)) if head and os.path.exists(head) else path
    _REAL[path] = out
    return out


_REPOS = {}


def repo_of(path):
    """The nearest folder at or above `path` holding a .git entry (a working copy or linked worktree), or None."""
    if not path:
        return None
    if path in _REPOS:
        return _REPOS[path]
    probe, found = path, None
    while probe and probe != os.path.dirname(probe):
        if os.path.exists(os.path.join(probe, ".git")):
            found = probe
            break
        probe = os.path.dirname(probe)
    _REPOS[path] = found
    return found


def _in_temp(path):
    return any(path == t or path.startswith(t + "/") for t in _TEMP) or bool(
        os.environ.get("TMPDIR") and path.startswith(os.path.normpath(os.environ["TMPDIR"])))


def _under(path, root):
    return root is None or path == root or path.startswith(root.rstrip("/") + "/")


def _redirect_targets(s):
    words = shell_words(s)
    out = []
    for i, (w, plain) in enumerate(words):
        if plain is None and re.match(r"^(?:1|&)?>{1,2}\|?$", w) and i + 1 < len(words) and words[i + 1][1]:
            target = words[i + 1][0]
            if not re.match(r"^&?\d$", target):
                out.append(target)
    return out


def _is_writer(s):
    """A command whose purpose is writing files: sed -i, tee, cp, mv, touch, rm, a redirect of cat/echo/printf..."""
    words = shell_words(s)
    if not words or words[0][1] is None:
        return False
    head = os.path.basename(_unq(words[0][0]) or "")
    flags = [_unq(w) or "" for w, plain in words[1:] if plain]
    if head == "sed":
        return any(re.match(r"^-[a-zA-Z]*i", f) or f.startswith("--in-place") for f in flags)
    if head == "perl":
        return any(re.match(r"^-[a-zA-Z]*i", f) for f in flags)
    if head in ("tee", "cp", "mv", "rsync", "install", "touch", "truncate"):
        return True
    if head == "rm":
        return not any(re.match(r"^-[a-zA-Z]*[rR]", f) or f == "--recursive" for f in flags)
    if head in ("cat", "echo", "printf", "jq", "awk", "sort", "head", "tail", "cut", "tr", "base64", "yq"):
        return any(not _in_temp(t) for t in (resolve_path(t) or t for t in _redirect_targets(s)))
    return False


def writes(seg, root=None, command=None):
    """'yes' if this part of a command changes files under `root` (anywhere outside temporary folders when root is
    None), 'maybe' if it might, None if it does not. `command` is the whole command line, for inline scripts."""
    s = seg.core or ""
    if not (set(seg.steps or ()) & _WRITE_KINDS) and ">" not in s and not _WRITER_HEAD.match(s):
        return None
    words = shell_words(s)
    if not words or words[0][1] is None:
        return None
    env, cur = seg.env or {}, seg.dir
    head = os.path.basename(_unq(words[0][0]) or "")
    args = [w for w, plain in words[1:] if plain]
    flags = [_unq(a) or "" for a in args]

    root = real_path(root) if root else root
    cur = real_path(cur) if cur else cur

    def at(raw_target, strength="yes"):
        path = resolve_path(raw_target, env, cur)
        if path is None:
            return "maybe" if (cur is None or _under(cur, root)) else None
        path = real_path(path)
        if _in_temp(path) or _GENERATED.search(path) or not _under(path, root):
            return None
        return strength

    def here(strength="yes", git=False):
        if cur is None:
            return "maybe"
        if _in_temp(cur):
            return None
        if git:  # git changes the working copy it runs in, not worktrees nested in its folder
            repo = repo_of(cur)
            return strength if (repo == root if (repo and root) else _under(cur, root)) else None
        return strength if _under(cur, root) else None

    found = []
    kinds = set(seg.steps or [])
    if kinds & {"git_update", "git_rewrite"} and not _GIT_NO_FILES.search(s):
        found.append(here(git=True))
    if _FORMATTER.match(s) or "edit" in kinds and re.match(r"^(?:npm|pnpm|yarn)\s+(?:version|pkg\s+set)\b", s):
        found.append(here())
    if "install" in kinds:
        found.append(here("maybe"))
    positional = [a for a, f in zip(args, flags) if not f.startswith("-")]
    if head == "sed" and _is_writer(s):
        files = positional[1:] if not any(f in ("-e", "--expression") for f in flags) else positional
        files = [a for a in files if (_unq(a) or "") != ""]
        found.extend(at(a) for a in files[-3:]) if files else found.append(here("maybe"))
    elif head == "perl" and _is_writer(s):
        found.extend(at(a) for a in positional[-3:]) if positional else found.append(here("maybe"))
    elif head in ("tee", "touch", "truncate", "rm") and positional:
        found.extend(at(a) for a in positional)
    elif head in ("cp", "rsync", "install", "ln") and len(positional) >= 2:
        found.append(at(positional[-1]))
    elif head == "mv" and len(positional) >= 2:
        found.extend(at(a) for a in positional)
    elif head == "patch" or (head in ("unzip", "tar") and (head == "unzip" or any("x" in f for f in flags[:1]))):
        found.append(here("maybe" if head != "patch" else "yes"))
    for t in _redirect_targets(s):
        found.append(at(t, "yes" if _is_writer(s) else "maybe"))
    if "inline_script" in kinds and command and _WRITE_API.search(command):
        literals = [real_path(resolve_path(p, env, cur)) for p in _PATH_LITERAL.findall(command)]
        literals = [p for p in literals if p and not _in_temp(p)]
        if literals:
            found.append("maybe" if any(_under(p, root) for p in literals) else None)
        else:
            found.append(here("maybe"))
    if "yes" in found:
        return "yes"
    return "maybe" if "maybe" in found else None


def command_steps(command, extra=None, depth=0):
    """Return the ordered list of step kinds a shell command performs (possibly empty)."""
    if not isinstance(command, str) or not command.strip():
        return []
    return [st for seg in plan(command, extra) for st in seg.steps]


def focus(command, steps, extra=None, depth=0):
    """The part of a shell command that performed one of `steps` (its raw text), or None."""
    wanted = set(steps or [])
    for seg in plan(command or "", extra):
        if wanted & set(seg.steps):
            return seg.raw
    return None


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
SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell", "CommandExecution")


def tool_plan(tool, command=None, extra=None, cwd=None):
    """(steps, parts) for a tool call; parts is the parsed shell command, empty for other tools."""
    if tool in SHELL_TOOLS:
        segs = plan(command, extra, cwd) if isinstance(command, str) and command.strip() else []
        return ([st for seg in segs for st in seg.steps] or ["other_command"]), segs
    return tool_steps(tool, command, extra), []


def tool_steps(tool, command=None, extra=None):
    """Steps for any tool call: shell commands are parsed; other tools map to one step."""
    if tool in SHELL_TOOLS:
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


def shape(segment):
    """A short, general form of a command, for grouping ones that recur: `./scripts/ship.sh`, `npm run release`."""
    words = [w for w, plain in shell_words(_peel(segment)) if plain]
    if not words:
        return ""
    first = _unq(words[0]) or words[0]
    if first in ("npm", "pnpm", "yarn", "bun") and len(words) >= 3 and words[1] == "run":
        rest = [w for w in words[2:] if not w.startswith("-")]
        return " ".join([first, "run"] + rest[:1])
    if first in ("node", "python", "python3", "bash", "sh", "zsh", "tsx", "ts-node", "deno", "ruby", "perl") and len(words) > 1:
        script = next((w for w in words[1:] if not w.startswith("-")), "")
        return "%s %s" % (first, _short_script(script)) if script else first
    if first in ("make", "just", "gh", "git", "docker", "kubectl", "wrangler", "cargo", "go", "uv", "poetry"):
        sub = next((w for w in words[1:] if not w.startswith("-")), "")
        return "%s %s" % (first, sub) if sub else first
    return _short_script(first)


def _short_script(word):
    w = (_unq(word) or word).replace(_HOME, "~")
    parts = w.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else w


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
