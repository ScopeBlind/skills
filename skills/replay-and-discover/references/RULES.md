# Rules file reference

A rules file is JSON. Each rule has a plain-English `says` sentence (what the person agreed) and a deterministic
definition underneath. The same file is used by `replay.py rehearse` (over past history) and by `enforce_hook.py`
(before each live tool call in Claude Code or Codex), so a rule means the same thing in both places.

```json
{
  "version": 1,
  "name": "How we ship",
  "live": true,
  "budget": {"asks_per_week": 10},
  "steps": {
    "live_check": ["curl .*https://(www\\.)?example\\.com", "playwright test .*smoke"],
    "deploy": ["\\./scripts/ship\\.sh\\b"],
    "test": ["node scripts/check-[\\w-]+\\.mjs"]
  },
  "rules": [
    {"id": "approve-releases", "type": "hold", "says": "A person approves every deploy and publish.",
     "match": {"steps": ["deploy", "publish"]}, "approval_minutes": 60},
    {"id": "client-approves-production", "type": "hold",
     "says": "The client approves each production deploy on the standard's page.",
     "match": {"steps": ["deploy"], "command": "--prod|production"}, "approver": "page"},
    {"id": "tests-before-merge", "type": "require_before",
     "says": "Tests and the type-check pass on the final version before a pull request is merged.",
     "match": {"steps": ["pr_merge"]}, "requires": ["test", "typecheck"], "passed": true, "fresh": true,
     "if_missing": "fix", "ignore": ["dist/**", "*.snap"]},
    {"id": "live-check", "type": "require_after", "says": "The live site is checked within 15 minutes of a deploy.",
     "match": {"steps": ["deploy"]}, "requires": ["live_check"], "within_minutes": 15},
    {"id": "migrations-need-approval", "type": "hold", "says": "A person approves any edit to a migration.",
     "match": {"steps": ["edit"], "path": "*/migrations/*"}},
    {"id": "two-deploys-a-day", "type": "limit", "says": "At most two production deploys a day.",
     "match": {"steps": ["deploy"], "command": "--prod|production"}, "max": 2, "per": "day"},
    {"id": "brand-voice", "type": "unsupported", "says": "Customer-facing copy matches our brand voice.",
     "note": "Judgement: a person reviews it."}
  ]
}
```

## File settings

| Field | Meaning |
|---|---|
| `live` | `false` switches the file off for every hook. The plugin's hook only enforces a file that says `true` (`replay.py enable`). |
| `budget.asks_per_week` | How often the rules may stop to ask a person (default 10). Rehearsal compares against it and lists ways to ask less. |
| `steps` | Your own commands, as regular expressions tested before the built-in patterns. The key is a built-in step (`deploy`, `test`...) or a new name you then use in rules. Add them with `replay.py label`. |

## Rule types

| Type | Meaning | Live hook |
|---|---|---|
| `hold` | A person approves before the action runs. `approval_minutes`: one approval covers the rule's other matching calls in the same session for that long. `"approver": "page"`: the person named on the standard decides on its page (see below). | Claude Code asks the person. Codex cannot ask, so the call is refused with the command the person can run in their own terminal to approve. With a page, the action waits there. |
| `block` | The action never runs. | Refused; the agent is told not to retry. |
| `require_before` | Every step in `requires` ran first: earlier in the session (within `within_minutes`, default 180), or earlier in the same command joined so that its failure would stop the action. `passed` (default true): it succeeded. `fresh`: it saw the final files. `if_missing`: `fix` (default), `hold` or `block`. `ignore`: file patterns that do not count as changes. | `fix`: refused with an exact instruction the agent can act on. `hold`: asks a person. `block`: refused. When the requirement cannot be met here (a repository with no tests, say), the person can let it go ahead with `replay.py approve <rule> --minutes 30`; the refusal shows the exact command. |
| `require_after` | At least one step in `requires` follows within `within_minutes` (default 30). | Never enforced live (nothing can prevent a step that has not happened yet). Reported by replay. |
| `limit` | At most `max` matching actions per `per` (`day`, default, or `session`). | Refused once the limit is reached. |
| `unsupported` | A sentence no deterministic check covers. | Listed only, never enforced or claimed. |

### How "passed" and "fresh" are decided

- **Joins.** In `npm test && git push` the push runs only if the tests passed, so the test counts. In
  `npm test; git push`, `npm test || true; git push` and `npm test 2>&1 | tail -5 && git push` it does not, because a
  failure would not stop the push. `set -e` counts for `;` and line breaks; `set -o pipefail` counts for pipes.
- **Hidden results.** When a pipe or a background run hides the exit status, the test runner's own summary line
  ("Tests 2 failed", "=== 10 passed in 1.2s ===", "# fail 0") decides. With no summary, the result is "can't tell",
  never a pass. The agent is told to run the tests without the pipe, or with `set -o pipefail`.
- **Fresh, live.** When a required check is about to run, the hook records the git working copy's content. Before
  the push or deploy, it compares: for a push, what is committed (untracked files that were never committed are left
  out); for anything else, the whole working copy. Any change counts, whoever made it: an edit tool, a shell command,
  another agent or a person. Undoing an edit is not a change. The refusal lists the changed files.
- **Fresh, in replay.** The log shows edits, file-changing commands (`sed -i`, redirects into the project, `git
  checkout`, `git pull`, formatters...) and edits by other sessions in the same folder. Writes to temporary folders
  and build output do not count. Checking out or pulling another version after the test is reported separately.

## The standard's page

`"approver": "page"` sends a held action to the standard's page on scopeblind.com, where the person named on the
standard approves or declines that exact command and signs the decision in their browser. The agent is told it is
waiting and retries the same command later; a changed command is a new request. Approvals are tied to the session and
the day. Connect a page with `replay.py connect --report <URL>` in your own terminal (it asks for the write token and
keeps it where only the hook reads it). `replay.py share` prepares the sentences to publish. Without a connected page,
these rules refuse the action and say why.

## `match` fields

All present fields must hold. At least one is required.

| Field | Meaning |
|---|---|
| `steps` | Any of these step kinds (table below, or your own from `steps`). |
| `command` | A regular expression on the shell command. With `steps`, it is tested only against the part of the command that performed those steps. |
| `tool` | A regular expression on the tool name (`Bash`, `Edit`, `apply_patch`, `mcp__github__create_pr`, ...). |
| `path` | A glob on the file an edit touches, for example `*/migrations/*` or `*.env`. |
| `secrets` | `true`: the command carries a literal secret (a token, key, password, or `NAME_TOKEN=value`). |
| `branch` | A regular expression on the git branch recorded with the call (Claude Code only). |

`scope` narrows a rule: `{"projects": ["my-app"], "agents": ["claude-code"]}`. Projects are matched as substrings
of the working directory.

## Built-in step kinds

| Kind | Label | Category |
|---|---|---|
| `test` | Run tests | check |
| `typecheck` | Type-check | check |
| `build` | Build | check |
| `lint` | Lint or format | check |
| `ci_check` | Check CI | check |
| `pr_review` | Review a pull request | check |
| `install` | Install dependencies | change |
| `edit` | Edit files (edit tools, `sed -i`, writing a file with a redirect, `cp`, `mv`, `touch`...) | change |
| `git_update` | Update files from git (`pull`, `merge`, `checkout <branch>`, `stash`, `cherry-pick`...) | change |
| `commit` | Commit | finish |
| `push` | Push | finish |
| `pr_open` | Open a pull request | finish |
| `pr_merge` | Merge a pull request | finish |
| `deploy` | Deploy | finish |
| `publish` | Publish a package or release | finish |
| `force_push` | Force-push | risky |
| `git_rewrite` | Rewrite or discard git history | risky |
| `delete` | Delete files recursively | risky |
| `db_change` | Change a database | risky |
| `cloud_change` | Change cloud resources | risky |
| `permissions` | Change permissions or run as root | risky |
| `secret_read` | Read secrets | risky |
| `web_request` | Call a URL | other |
| `run_script` | Run a project script | other |
| `inline_script` | Run an inline script (`python3 -c`, `node -e`, a heredoc) | other |
| `inspect` | Look at files or state (`ls`, `cat`, `rg`, `git status`...) | other |
| `read` | Read files | other |
| `web` | Browse or search the web | other |
| `mcp` | Use a connected tool | other |
| `delegate` | Hand work to a sub-agent | other |
| `other_command` | Other command | other |

A dry run (`--dry-run`, `git push -n`) is not the real action. A command someone only mentions, inside a commit
message, an `echo` or a heredoc, is not a step. Steps inside `if`/`then`, `xargs` and `zsh -c '...'` are seen.
