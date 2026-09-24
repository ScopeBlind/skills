---
name: replay-and-discover
description: Replay the local history of your coding agents (Claude Code and Codex) to see how your work actually flows. It finds your recurring workflows, the definition of done you actually practise and its exceptions, what you can already show about past work, consequential actions, secrets in commands, friction and rework. Then it interviews you, writes what you want as a plain-English standard backed by deterministic rules, rehearses it against the same history, and can enforce it live with a Claude Code hook. Everything runs locally and nothing is sent anywhere. Use when someone asks how their agents really work, what their definition of done is, what they could prove about work already done, where approvals or checks belong, or wants to set rules for their agents.
license: MIT
compatibility: Python 3.8 or newer (standard library only). Reads ~/.claude/projects and ~/.codex/sessions on this machine. The optional GitHub check needs the gh CLI, signed in.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py *)
metadata:
  author: ScopeBlind
  version: "0.1.0"
---

# Replay and discover

Your agents already keep a detailed record of every command, edit and check they ran. This skill reads that record
on this machine, shows how the work actually flows, and turns what the person wants into rules their agents follow.

The order matters: first what they can already show, then the gaps, then a short interview grounded in their own
numbers, then a standard they confirm, rehearse and (only if they say so) switch on.

## Privacy: say this before the first scan

- It reads Claude Code transcripts (`~/.claude/projects`) and Codex sessions (`~/.codex/sessions`) on this machine.
- It writes a report to `~/.scopeblind/replay/<timestamp>/`, readable only by this user.
- Nothing is sent anywhere. The optional GitHub check reads merged pull requests with the person's own `gh` login,
  only if they agree, and reports counts only.
- Examples are redacted: tokens, passwords and keys are replaced, and the home directory is shown as `~`.

Ask before scanning (one AskUserQuestion call): how far back (default 30 days; Claude Code keeps transcripts for its
`cleanupPeriodDays` setting, 30 days by default), and all projects or only this one. If the person invoked the skill
with arguments (`$ARGUMENTS`, for example `14 days` or a project name), use them and skip that question.

## Steps

### 1. Scan

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 30
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 30 --project my-app
```

It prints the full report as Markdown and ends with the paths of `report.html` (visual) and `findings.json`.
If it finds no history, say which folders it checked and stop.

### 2. Show what they can already show, then the gaps

Summarise in about 15 lines, in this order. Use their numbers; do not paste the whole report.

1. **What you can already show** (section 1): for pushes, pull requests, merges, deploys and publishes, how many
   had a passing test before, how many tested the final version, and how many were checked afterwards. Always say
   the strength: this is the agent's own log, which is self-reported.
2. **How your work flows** (section 2): the top two or three recurring workflows.
3. **Your definition of done, as practised** (section 3): the usual checks before each step and how many went
   ahead without them.
4. **The two or three suggestions that matter most** (section 6), each with its evidence.

If commands carried literal secrets (section 4), say so plainly and recommend rotating those secrets and moving
them to environment variables or a secret manager: they are now stored in the agent's local history.

Offer to open the visual report (`open <report.html>` on macOS, `xdg-open` on Linux).

### 3. Optional: stronger evidence from GitHub

Ask first. If they agree:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 30 --github --quiet
```

It reads, per repository seen in the history, the merged pull requests in the window: how many were approved by
someone other than the author and how many had every check green. This is kept by GitHub, which the agent does not
control, so it is stronger than the agent's log. Report counts only.

### 4. Interview

One AskUserQuestion call with at most four questions, each grounded in a finding and each offering "keep it as it
is". Pick from [references/INTERVIEW.md](references/INTERVIEW.md). Typical round:

- Their usual checks before a push or merge were skipped N times: make them a rule, only for main, or warn only?
- Which consequential actions should wait for a person (multi-select: deploy, publish, merge, database changes...)?
- Should every deploy be followed by a live check within 30 minutes?
- What must someone else (a contractor, another team, another company's agent) show before you accept their work?

### 5. Write the standard

Write it in two parts, both in plain English first:

- **For my own work**: what the person's agents must do.
- **What I require from others**: what people, vendors or agents who send them work must show.

Then encode each sentence as a rule in a rules file (format: [references/RULES.md](references/RULES.md)). Start
from the suggestions and edit:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py init-rules --from ~/.scopeblind/replay/<timestamp>/findings.json --out .claude/scopeblind-rules.json
```

Ask where to keep it: this project (`.claude/scopeblind-rules.json`) or all projects (`~/.scopeblind/rules.json`).
A sentence no deterministic check covers (for example "the copy is on brand") becomes a rule of type `unsupported`:
it is listed, never claimed as checked. The "require from others" part is a checklist to send them; this skill can
only check it against their records if they share them.

### 6. Read it back and rehearse

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py explain --rules .claude/scopeblind-rules.json
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py rehearse --rules .claude/scopeblind-rules.json --days 30
```

Show the readback table (each sentence, how it is checked, whether a live hook enforces it) and ask them to confirm
it means what they meant. Then show the rehearsal: what each rule would have held, refused or reported over the
same history, with examples, and the cost in holds per week. Adjust the rules with them until the cost and the
catches look right, and rehearse again.

### 7. Turn it on (only with a clear yes)

The hook enforces `hold` (Claude Code asks the person), `block` (refused), `require_before` (asks or refuses when a
required step did not run, did not pass, or ran before the last edit) and `limit`. `require_after` rules are never
enforced live; they are reported by the next replay. Say this before asking.

Show the exact change first, then add it to the settings file they choose (`.claude/settings.json` for this project,
`~/.claude/settings.json` for all), merging with any hooks already there, never replacing them. Use absolute paths:

```json
{"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
  "command": "python3 /ABSOLUTE/PATH/TO/skill/scripts/enforce_hook.py --rules /ABSOLUTE/PATH/TO/scopeblind-rules.json"}]}]}}
```

Every decision is appended to `~/.scopeblind/replay/decisions.jsonl`. To turn it off, remove that hook entry. If the
hook ever fails, it makes no decision, so Claude Code's normal permissions apply.

### 8. Next time

Suggest a weekly replay that compares against the last one:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/replay.py scan --days 7 --compare ~/.scopeblind/replay/<earlier>/findings.json
```

## Honesty rules

- The agent's log is self-reported. Never call it verified, audited or proven. GitHub's records are stronger;
  say which is which.
- "Can't tell" (a result the log did not record) is never counted as passing.
- Only `hold`, `block`, `require_before` and `limit` are enforced live. Never describe `require_after` or
  `unsupported` rules as enforced.
- Suggestions are drafts. The person decides every rule and every threshold.
- Never print a secret value, even if asked; the scripts redact them and so should you.
- The skill cannot see work done outside these agents (the person's own terminal, CI, other people).

## Proving it to someone else

A local record is good for improving your own work. When a client, auditor or counterparty needs to check the work
themselves, they need evidence the agent does not control: for example a gate that signs a receipt for every call,
with the rules on a page they can open (see the `put-a-standard-in-force` skill in this repository). Mention this
only if the person asks about proving or sharing.

## Limits

- Reads Claude Code and Codex CLI history. Other agents are not read yet.
- Steps are recognised from command patterns (tests, builds, commits, pushes, deploys, publishes, deletes and more);
  add patterns for your own tools under `steps` in the rules file.
- Durations come from timestamps in the log and are approximate.
