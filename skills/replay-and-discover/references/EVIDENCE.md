# How strong is each piece of evidence?

Say which kind every finding is. Do not upgrade it.

| Strength | Source | What it shows | What it does not show |
|---|---|---|---|
| Self-reported | The agent's own log (Claude Code transcripts, Codex sessions). This is everything `replay.py scan` reads. | What the agent recorded doing, in order, with the results it recorded. | That the record is complete or unaltered: the agent, or anyone with access to the machine, could have changed it. Work done outside the agent. |
| Kept by a third party | GitHub's record of a merged pull request (reviews, check runs), read with `--github`; a CI provider's run history. | That a named person other than the author approved, and that the checks the platform ran passed. | What the agent did before the pull request, or on other routes. |
| Enforced and signed | A gate the agent cannot bypass that signs a receipt for every call it allows or refuses (for example protect-mcp with a standard in force). | That every call through that gate was checked against the rules in force, with a tamper-evident record. | Calls that did not go through the gate. |

Rules of thumb:

- A local replay is for improving your own work. It is honest to say "your agents' logs show"; it is not honest to
  say "verified" or "proven".
- "Can't tell" means the log did not record the result. It is never a pass.
- When someone else must rely on the work, move up the table: evidence kept by a party the agent does not control,
  or a gate that signs what it allowed.
