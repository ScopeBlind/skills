# How strong is each piece of evidence?

Say which kind every finding is. Do not upgrade it.

| Strength | Source | What it shows | What it does not show |
|---|---|---|---|
| Self-reported | The agents' own logs (Claude Code transcripts, Codex sessions), which is everything `replay.py scan` reads; and the hook's own decisions log. | What the agent recorded doing, in order, with the results it recorded, and what the hook allowed, asked or refused. | That the record is complete or unaltered: the agent, or anyone with access to the machine, could have changed it. Work done outside the agents. |
| Kept by a third party | GitHub's record of a merged pull request (reviews, check runs), read with `--github`; a CI provider's run history. | That a named person other than the author approved, and that the checks the platform ran passed. | What the agent did before the pull request, or on other routes. |
| Signed by the person who decided | The standard's page on scopeblind.com, for rules with `"approver": "page"`. | Each held command, who decided it, when, and their Ed25519 signature over that exact action, which the page checked against the keys the standard names. Anyone with the link can read it. | The agent's other calls, which never reach the page, and whether the command that ran afterwards was the one approved (the local hook checks that; the page cannot). |
| Enforced and signed | A gate the agent cannot bypass that signs a receipt for every call it allows or refuses (protect-mcp with a standard in force; see the `put-a-standard-in-force` skill). | That every call through that gate was checked against the rules in force, with a tamper-evident record. | Calls that did not go through the gate. |

Rules of thumb:

- A local replay is for improving your own work. It is honest to say "your agents' logs show"; it is not honest to
  say "verified" or "proven".
- A range is a range: "30% to 48%" means the log settled some cases and not others. Say why.
- "Can't tell" means the log did not record the result. It is never a pass.
- The live hook's file comparison is exact about content, but it is still this machine's own check: self-reported
  to anyone else.
- When someone else must rely on the work, move down the table: evidence kept by a party the agent does not control,
  a decision signed by the person who made it, or a gate that signs what it allowed.
