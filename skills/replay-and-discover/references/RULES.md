# Rules file reference

A rules file is JSON. Each rule has a plain-English `says` sentence (what the person agreed) and a deterministic
definition underneath. The same file is used by `replay.py rehearse` (over past history) and by `enforce_hook.py`
(before each live tool call in Claude Code), so a rule means the same thing in both places.

```json
{
  "version": 1,
  "name": "How we ship",
  "steps": {"live_check": ["curl .*https://(www\\.)?example\\.com", "playwright test .*smoke"]},
  "rules": [
    {"id": "approve-releases", "type": "hold", "says": "A person approves every deploy and publish.",
     "match": {"steps": ["deploy", "publish"]}},
    {"id": "tests-before-merge", "type": "require_before",
     "says": "Tests and the type-check pass on the final version before a pull request is merged.",
     "match": {"steps": ["pr_merge"]}, "requires": ["test", "typecheck"], "passed": true, "fresh": true,
     "if_missing": "hold"},
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

## Rule types

| Type | Meaning | Live hook |
|---|---|---|
| `hold` | A person approves before the action runs. | Claude Code asks the person. |
| `block` | The action never runs. | Refused. |
| `require_before` | Every step in `requires` ran earlier in the session, within `within_minutes` (default 180). `passed` (default true): its last run succeeded. `fresh`: it ran after the last file edit. `if_missing`: `hold` (default) or `block`. | Asks or refuses when missing; asks when the result was not recorded. |
| `require_after` | At least one step in `requires` follows within `within_minutes` (default 30). | Never enforced live (nothing can prevent a step that has not happened yet). Reported by replay. |
| `limit` | At most `max` matching actions per `per` (`day`, default, or `session`). | Refused once the limit is reached. |
| `unsupported` | A sentence no deterministic check covers. | Listed only, never enforced or claimed. |

## `match` fields

All present fields must hold. At least one is required.

| Field | Meaning |
|---|---|
| `steps` | Any of these step kinds (table below). |
| `command` | A regular expression on the shell command. With `steps`, it is tested only against the part of the command that performed those steps. |
| `tool` | A regular expression on the tool name (`Bash`, `Edit`, `mcp__github__create_pr`, ...). |
| `path` | A glob on the file an edit touches, for example `*/migrations/*` or `*.env`. |
| `secrets` | `true`: the command carries a literal secret (a token, key, password, or `NAME_TOKEN=value`). |
| `branch` | A regular expression on the git branch recorded with the call (Claude Code only). |

`scope` narrows a rule: `{"projects": ["my-app"], "agents": ["claude-code"]}`. Projects are matched as substrings
of the working directory.

## Custom steps

`steps` at the top of the file adds your own step kinds, as regular expressions tested against each command before
the built-in patterns. Use them for your own release scripts, smoke tests or live checks, then refer to them in
`requires` or `match.steps`.

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
| `edit` | Edit files | change |
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
| `read` | Read files | other |
| `web` | Browse or search the web | other |
| `mcp` | Use a connected tool | other |
| `delegate` | Hand work to a sub-agent | other |
| `other_command` | Other command | other |

A dry run (`--dry-run`, `git push -n`) is not the real action. A command someone only mentions, inside a commit
message, an `echo` or a heredoc, is not a step.
