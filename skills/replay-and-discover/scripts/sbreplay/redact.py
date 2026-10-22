"""Keep secrets and personal paths out of anything the report shows.

Commands in an agent's history can contain tokens, passwords and private URLs. Every example that leaves the parser
passes through `redact`, and the rule engine uses `find_secrets` to flag commands that carried a literal secret.
"""
import os
import re

_HOME = os.path.expanduser("~")

# Specific credential formats. Each entry is (label, pattern). Order matters: the more specific formats come first.
_SECRET_FORMATS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("api key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("stripe key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("aws key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("password in url", re.compile(r"://[^/\s:@'\"]+:[^/\s@'\"]{3,}@")),
    ("authorization header", re.compile(r"(?i)authorization\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{12,}")),
]

# A literal value assigned to a secret-looking name, for example API_TOKEN=abc123... (not API_TOKEN=$VAR).
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CLIENT_?SECRET)[A-Z0-9_]*)"
    r"=(\"[^\"$]{8,}\"|'[^'$]{8,}'|[^\s\"'$][^\s]{7,})"
)
_FLAG = re.compile(r"(?i)(--?(?:token|password|passwd|secret|api-?key|access-?key|client-?secret)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)")
_KEYVAL = re.compile(
    r"(?i)([\"']?(?:password|passwd|secret|token|api_?key|access_?key|client_?secret)[\"']?\s*[:=]\s*)"
    r"(\"[^\"]{6,}\"|'[^']{6,}'|[^\s,;})\"'$][^\s,;})\"']{5,})"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{12,}")
# Long mixed-case alphanumeric runs are usually keys or signatures; replace them in examples.
_LONG = re.compile(r"(?<![/\w])(?=[A-Za-z0-9+_-]{40,})(?=\S*[A-Z])(?=\S*[a-z])(?=\S*[0-9])[A-Za-z0-9+_-]{40,}")


# For detection (not display): an environment-style NAME=value with a literal value, in the command itself (not in a
# heredoc body, which is usually code or data). Placeholders, numbers, paths and variable references do not count.
_ENV_SECRET = re.compile(
    r"(?:^|[\s;&|(])([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CLIENT_SECRET)"
    r"[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)
_PLACEHOLDER = re.compile(r"(?i)^(?:x+|\*+|changeme|change-me|dummy|example|placeholder|redacted|secret|password|token|test|"
                          r"<[^>]*>|\[[^\]]*\]|\.{3}|none|null|true|false)$")


def _real_value(value):
    v = value.strip("\"'")
    if len(v) < 8 or v.startswith(("$", "/", "~", ".", "http://", "https://", "file:")):
        return False
    if v.isdigit() or _PLACEHOLDER.match(v):
        return False
    return True


# For detection only: a private key counts when a whole PEM block is present (header, a body of key material, footer),
# not when code merely mentions the header, and credentials in a URL count unless they are an obvious placeholder.
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*(?:\\n|\s)*[A-Za-z0-9+/=\s\\n]{64,}?-----END [A-Z ]*PRIVATE KEY-----")
_URL_CREDENTIALS = re.compile(r"://([^/\s:@'\"]+):([^/\s@'\"]{3,})@([^/\s'\"?#]*)")
_PLACEHOLDER_HOST = re.compile(r"(?i)(?:^|\.)(?:example\.(?:com|org|net)|invalid|test|localhost)(?::\d+)?$")
_PLACEHOLDER_PASS = re.compile(r"(?i)^(?:pass(?:word)?|pw|pwd|secret|x+|\*+|changeme|test|dummy|p|u)$")


def _real_url_credentials(text):
    for m in _URL_CREDENTIALS.finditer(text):
        user, password, host = m.groups()
        if _PLACEHOLDER_PASS.match(password) or _PLACEHOLDER_HOST.search(host) or password.startswith(("$", "${")):
            continue
        return True
    return False


def find_secrets(text):
    """Return the labels of literal secrets in `text` (empty list if none). References like $TOKEN do not count."""
    if not isinstance(text, str) or not text:
        return []
    found = []
    for label, rx in _SECRET_FORMATS:
        if label == "private key":
            if _PEM_BLOCK.search(text):
                found.append(label)
        elif label == "password in url":
            if _real_url_credentials(text):
                found.append(label)
        elif rx.search(text):
            found.append(label)
    from .classify import strip_heredocs
    command = strip_heredocs(text)
    if any(_real_value(m.group(2)) for m in _ENV_SECRET.finditer(command)):
        found.append("secret in an environment variable")
    return found


def redact(text, limit=160):
    """Redact secrets, shorten the home directory to ~, collapse whitespace and trim to `limit` characters."""
    if not isinstance(text, str):
        return ""
    t = text
    for label, rx in _SECRET_FORMATS:
        t = rx.sub("[" + label + "]", t)
    t = _ASSIGNMENT.sub(lambda m: m.group(1) + "=[redacted]", t)
    t = _FLAG.sub(lambda m: m.group(1) + "[redacted]", t)
    t = _KEYVAL.sub(lambda m: m.group(1) + "[redacted]", t)
    t = _BEARER.sub(lambda m: m.group(1) + "[redacted]", t)
    t = _LONG.sub("[redacted]", t)
    if _HOME and len(_HOME) > 1:
        t = t.replace(_HOME, "~")
    t = " ".join(t.split())
    if limit and len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def short_path(path):
    """Show a working directory with the home directory as ~, for project names in reports."""
    if not isinstance(path, str) or not path:
        return "(unknown)"
    if _HOME and path.startswith(_HOME):
        return "~" + path[len(_HOME):]
    return path
