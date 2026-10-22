---
name: replay-and-discover
description: Replay the local history of your coding agents (Claude Code and Codex) to see how your work actually flows and what you can already show about it, including whether each release shipped the version your tests actually ran on, your definition of done and its exceptions, secrets in commands, friction and rework. Then it interviews you, writes what you want as plain-English rules with deterministic checks, rehearses them against the same history, and enforces them live with a hook for Claude Code or Codex, where the agent is told exactly what to fix and only real approvals reach you. When someone else must rely on the work, a named person can approve held actions on your standard's page. Runs locally. Use when someone asks how their agents really work, what their definition of done is, what they could prove about work already done, where approvals or checks belong, or wants rules for their agents.
license: MIT
compatibility: Python 3.8 or newer (standard library only) and git. Reads ~/.claude/projects and ~/.codex/sessions on this machine. The optional GitHub check needs the gh CLI, signed in.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py *)
metadata:
  author: ScopeBlind
  version: "0.2.0"
---

# Replay and discover

Your agents already keep a record of every command, edit and check they ran. This skill reads that record on this
machine, shows the person how their work actually flows, and turns what they want into rules their agents follow.

The order matters: what they can already show, then the gaps, then a short interview grounded in their own numbers,
then rules they confirm, rehearse and (only if they say so) switch on.

## Privacy: say this before the first scan

- It reads Claude Code transcripts (`~/.claude/projects`) and Codex sessions (`~/.codex/sessions`) on this machine.
- It writes reports to `~/.scopeblind/replay/`, readable only by this user.
- Nothing is sent anywhere. Two things can send data, and only if the person turns them on: the GitHub check (reads
  merged pull requests with their own `gh` login and reports counts) and a connected standard's page (a held command
  goes to the page so a named person can decide it).
- Examples are redacted: tokens, passwords and keys are replaced, and the home directory is shown as `~`.

Ask before scanning (one AskUserQuestion call): how far back (default 30 days; Claude Code keeps transcripts for its
`cleanupPeriodDays` setting, 30 days by default), and all projects or only this one. If the person invoked the skill
with arguments (`$ARGUMENTS`, for example `14 days` or a project name), use them and skip that question.

## 1. Scan, and lead with the first screen

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 30
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 30 --project my-app
```

It takes 10 to 40 seconds and prints the first screen: one sentence, up to three findings and one decision, then the
paths of the full report. Show that screen as it is. Offer the visual report (`open <report.html>` on macOS,
`xdg-open` on Linux). Read `report.md` for detail only when the person asks or the interview needs it. If it finds no
history, say which folders it checked and stop.

What the lead number means: "shipped a version that a passing test had provably run on" is a range. The low end
counts only releases where the log shows a passing result and no change to the files between the test and the
release. The high end adds the ones the log cannot settle, such as a test piped into `tail` with no summary line, or a
command that may have changed files. The reasons are listed as "Why not" in section 3 of the report. Report the range
as a range.

If commands carried literal secrets, say so plainly and recommend rotating those secrets and moving them to
environment variables or a secret manager: they are now stored in the agents' local history.

## 2. Teach it the person's own commands

Section 6 of the report lists commands that recur but match no known step, such as `node scripts/check-release.mjs`
or `./scripts/ship.sh`. These are often the person's real release and check steps, and no rule can see them until
they are labelled.

Propose a label for each of the top few from its name and example: a check or test script is `test`, a deploy or
release script is `deploy` or `publish`, a build is `build`. Ask the person to confirm them in one AskUserQuestion
call (multiSelect). Labels live in the rules file, so write one first (step 4) if there is none. For each confirmed
label, preview it, then save it, then rescan:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py label --rules .claude/scopeblind-rules.json --as test --pattern 'check-[\w-]+\.mjs' --dry-run
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py label --rules .claude/scopeblind-rules.json --as test --pattern 'check-[\w-]+\.mjs'
```

The preview shows how many past commands the pattern matches, in how many sessions, and what they were read as before.

## 3. Optional: stronger evidence from GitHub

Ask first. If they agree:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 30 --github --quiet
```

It reads, per repository seen in the history, the merged pull requests in the window: how many were approved by
someone other than the author and how many had every check green. GitHub keeps this record, and the agent does not
control it, so it is stronger evidence than the agent's log. Report counts only.

## 4. Interview, then write the rules

One AskUserQuestion call with at most four questions, each quoting a finding and each offering "keep it as it is".
Pick them from [references/INTERVIEW.md](references/INTERVIEW.md).

Write the rules in two parts, plain English first:

- **For my own work**: what the person's agents must do.
- **What I require from others**: what people, vendors or agents who send them work must show.

Then encode each sentence as a rule ([references/RULES.md](references/RULES.md)), starting from the suggestions:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py init-rules --from ~/.scopeblind/replay/<timestamp>/findings.json --out .claude/scopeblind-rules.json
```

Ask where to keep it: this project (`.claude/scopeblind-rules.json`) or all projects (`~/.scopeblind/rules.json`).
Three defaults keep rules livable; explain them when they come up:

- A `require_before` rule refuses with an exact instruction to the agent (`"if_missing": "fix"`), for example "these
  files changed after the tests last passed: src/a.ts; run `npm test` again, then retry". The agent fixes it itself.
  Use `"hold"` only when a person must look.
- A `hold` rule takes `"approval_minutes": 60`, so one approval covers the rest of the task.
- `"fresh": true` means the check must have seen the final files. Live, the hook compares the files themselves.

A sentence no deterministic check covers (for example "the copy is on brand") becomes a rule of type `unsupported`:
it is listed, never claimed as checked. The "require from others" part is a checklist to send them; this skill can
only check it against their records if they share them.

## 5. Read it back and rehearse

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py explain --rules .claude/scopeblind-rules.json
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py rehearse --rules .claude/scopeblind-rules.json --days 30
```

Show the readback table (each sentence, how it is checked, whether a live hook enforces it) and ask them to confirm
it means what they meant. Then show the rehearsal: for each rule, how often it would have asked a person, told the
agent to fix something, or refused, with examples; and the asks per week against the budget (10 unless the file sets
`"budget": {"asks_per_week": N}`). If it is over, go through the listed ways to ask less with the person (an approval
window, holding only production, telling the agent instead of asking) and rehearse again.

## 6. Turn it on (only with a clear yes)

Say what will happen before asking:

- In Claude Code, refusals tell the agent what to do, and holds appear as Claude Code's own approval prompt.
- Codex cannot ask. A hold refuses the call and tells the agent to ask the person, who approves from their own
  terminal with the command the refusal shows (`replay.py approve <rule> --minutes 30 --session <id>`). The agent
  cannot run that approval itself.
- When a refusal asks for something that cannot be done here (tests in a repository that has none, say), the
  person can let that one go ahead from their own terminal with the command the refusal shows.
- `require_after` and `unsupported` rules are never enforced live; the next replay reports them.

Show the exact change first, then install. `--agent both` covers Claude Code and Codex, and `--scope project` installs
for this repository only. It merges with existing hooks, backs up the file it changes, and installing twice leaves
one copy.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py install --agent both --dry-run
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py install --agent both
```

Codex runs a new hook only after the person trusts it: they review it under `/hooks` the next time they start Codex.
`--trust` records the trust in `~/.codex/config.toml` instead; ask before using it, because it changes Codex's
configuration.

With the ScopeBlind plugin installed, skip `install`: the plugin's hook enforces a rules file once it says
`"live": true` (`replay.py enable --rules <file>`).

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py status
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py uninstall --agent both
```

Every decision is appended to `~/.scopeblind/replay/decisions.jsonl`. The hook protects itself: its state is off
limits to the agent, and changes to the rules file or to agent settings wait for the person. It is a guardrail for an
agent working in good faith, not a security boundary against a hostile one. If the hook fails, it makes no decision,
so the agent's normal permissions apply.

## 7. When someone else must rely on it

A local record is self-reported. When a client, a manager or another company must rely on the work, a named person can
decide held actions on the standard's page on scopeblind.com, and their signed decision can be checked by anyone with
the link. Offer this only when the person asks about proving, sharing, or someone else approving.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py share --rules .claude/scopeblind-rules.json
```

It writes the sentences to paste at scopeblind.com/write, the steps to publish the page, and a redacted call log for
the page's Rehearse tab (read in the browser, not uploaded). The person connects the page in their own terminal, so the
write token never passes through the chat: `python3 <this skill>/scripts/replay.py connect --report '<report URL>'`.
Then add `"approver": "page"` to the hold rules that need it. Held commands go to the page; the agent carries on with
other work and retries once the person has decided.

Be exact about what the page holds: each held command, who decided it, when, and their signature. It does not receive
the agent's other calls. For a signed receipt of every call, run the session behind protect-mcp (the
`put-a-standard-in-force` skill in this repository).

## 8. Every week

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py weekly on
```

With the plugin installed, a new digest is prepared in the background once a week and shown at the start of the next
session: what changed since last week, what the hook decided, and which rules never came up (candidates to drop).
Without the plugin, run `replay.py digest` when they ask.

## Honesty rules

- The agents' log is self-reported. Never call it verified, audited or proven. GitHub's records and a page's signed
  decisions are stronger; say which is which ([references/EVIDENCE.md](references/EVIDENCE.md)).
- Report ranges as ranges. "Can't tell" is never counted as passing.
- Only `hold`, `block`, `require_before` and `limit` are enforced live. Never describe `require_after` or
  `unsupported` rules as enforced.
- Suggestions and labels are drafts. The person decides every rule, label and threshold.
- Never print a secret value, even if asked, and never ask for the page's write token in the chat.
- The skill cannot see work done outside these agents (the person's own terminal, CI, other people).

## Limits

- Reads Claude Code and Codex CLI history. Other agents are not read yet.
- Steps are recognised from command patterns, from how commands are joined (`&&`, `;`, pipes, `set -e`), and from a
  test runner's own summary line. The person's own scripts need labels.
- The live freshness check compares file content in git working copies; elsewhere it falls back to the log.
- Durations come from timestamps in the log and are approximate.
