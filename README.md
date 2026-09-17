# ScopeBlind skills

Installable skills for coding agents that put a person's limits in front of an
agent's work and keep a record both sides can check. Built on the open
[agent-skills spec](https://agentskills.io); works with Claude Code, Codex,
Cursor, OpenCode, Gemini CLI, and the other agents that read `SKILL.md`.

| Skill | What it does | Install |
|---|---|---|
| [`governed-monid`](skills/governed-monid/) | Put a signed budget, an endpoint allowlist, and a human hold in front of `monid run`, with a receipt for every call. | `npx skills add ScopeBlind/skills -s governed-monid` |
| [`put-a-standard-in-force`](skills/put-a-standard-in-force/) | Write the rules for an agent in plain English, sign them, and run any MCP server or Claude Code session behind the gate. | `npx skills add ScopeBlind/skills -s put-a-standard-in-force` |
| [`request-client-review`](skills/request-client-review/) | Turn a real pull request into a client review: brief, criteria, exact approvals, receiver-applied change, accepted result. | `npx skills add ScopeBlind/skills -s request-client-review` |
| [`check-a-record`](skills/check-a-record/) | Check a record someone sent you, in the browser or on the command line, without uploading it. | `npx skills add ScopeBlind/skills -s check-a-record` |

```bash
npx skills add ScopeBlind/skills --list
npx skills add ScopeBlind/skills -s governed-monid -a claude-code
```

Every skill is standalone: clone the repository and read its `SKILL.md`, or run
its bundled script. Nothing here needs an account. The open gate
([protect-mcp](https://www.npmjs.com/package/protect-mcp), MIT) and the open
verifier ([@veritasacta/verify](https://www.npmjs.com/package/@veritasacta/verify),
Apache-2.0) are free to run inside your own trust boundary. When a decision or a
record has to cross to someone else, the standard's page on
[scopeblind.com](https://scopeblind.com) is where they decide and where the
record lands.

## Contributing

Open a pull request with a new folder under `skills/`. The folder name must
match the `name` in the `SKILL.md` frontmatter. Keep `SKILL.md` under 500
lines and put reference material in `references/`. Do not commit keys, tokens,
or a real Monid balance.
