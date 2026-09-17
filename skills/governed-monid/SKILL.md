---
name: governed-monid
description: Put a signed budget, an endpoint allowlist, and a human hold in front of Monid tool calls, with a signed receipt for every call. Use when an agent will spend a Monid balance (monid run) on someone else's behalf, when a person must approve spend above a threshold, or when the operator wants a checkable record of which paid endpoints an agent used.
license: MIT
compatibility: Node.js 20 or newer, the Monid CLI (npm install -g @monid-ai/cli), and network access to scopeblind.com for holds
metadata:
  author: ScopeBlind
  version: "0.1.0"
---

# Governed Monid

Monid gives an agent one key to thousands of billable tools. This skill puts a
person's limits in front of that key. The agent still uses Monid's three verbs.
Every `run` passes through a gate that enforces a signed standard: only the
named tools, at most a per-call limit, and a hold on the standard's page when a
call is above the approval threshold. The person decides on their phone; the
agent retries the same call and it goes through, with a receipt that records
who decided.

Two layers check the money, and they are different in kind:

- **The gate** (protect-mcp, MIT) enforces the signed standard: the tool
  allowlist, the per-call limit, and the hold. It signs a chained receipt for
  every call, allowed or refused, and posts it to the standard's page.
- **The server in this skill** (code you run) checks that the price the agent
  states covers the endpoint's published price, and keeps a per-run budget in a
  local ledger. Monid's own metering is the billing truth; the ledger is the
  agent's view of it.

Discover and inspect are free at Monid. Run spends the user's balance.

## One-time setup

1. Install the Monid CLI and let the user add their key. The user creates the
   key; never paste or store it yourself.

   ```bash
   npm install -g @monid-ai/cli@latest
   monid keys list
   ```

2. In the working folder, create the gate's signing key and configuration once:

   ```bash
   npx -y protect-mcp@0.24.1 init
   ```

3. Get a signed standard and its compiled policy. Either use the bundled sample
   (three tools, at most USD 1.00 per call, a person approves above USD 0.25)
   or write your own at <https://scopeblind.com/write>: say the work, name the
   three tools, set the per-call limit and the approval threshold, sign it, and
   download `standard.json` and `policy/standard.cedar`.

   ```bash
   cp assets/standard.json ./standard.json
   mkdir -p policy && cp assets/policy/standard.cedar policy/standard.cedar
   ```

4. Publish the standard's page. On the Sign tab, choose "Publish this
   standard's page". You get a link for the person who decides, a report URL,
   and a write token shown once. Put the token in the environment where the
   gate runs and nowhere else:

   ```bash
   export PROTECT_MCP_REPORT_TOKEN='<the token from the Sign tab>'
   ```

   Without a page, calls above the threshold are held with no one to decide
   them. The person who signed the standard decides on the page.

5. Register the governed server with your agent. For Claude Code:

   ```bash
   claude mcp add governed-monid -- npx -y protect-mcp@0.24.1 --enforce \
     --cedar ./policy --standard ./standard.json \
     --report 'https://scopeblind.com/api/standard?s=<standard id>' \
     -- node scripts/governed-monid-server.mjs
   ```

   Any MCP client works: the command after `--` is the server, and everything
   before it is the gate.

## Using it

1. `monid_discover` with a plain description of what you need. Free.
2. `monid_inspect` the candidate. Read the price and the input shape. Free.
3. `monid_run` with the endpoint's inputs and the highest price you accept for
   this call as `amount_minor` in US cents, taken from inspect. Say the ceiling
   honestly; the server refuses a ceiling below the published price.

What can come back:

- **Allowed.** The result, the cost Monid reported, and the remaining run
  budget.
- **Refused by the standard.** The tool is not one of the three, or the
  ceiling is above the per-call limit. Do not retry the same call; report it.
- **Held.** `REQUIRES_APPROVAL` with the page address. Tell the user the person
  named on the standard needs to decide there. Continue other work. Retry the
  exact same call once they have decided; a changed call is a new action.
- **Refused by the server.** `ceiling_below_price` or `run_budget_exhausted`.
  Inspect again, or ask the person for a new budget.

## What is recorded

- `.protect-mcp-receipts.jsonl` in the working folder: one signed, chained
  receipt per call, with the tool, the decision, the reason, the stated
  ceiling, and, on a call a person decided, their decision.
- The standard's page: the same receipts, the held actions, and the decisions.
- `.governed-monid-ledger.json`: the run budget and each counted call.

Anyone the user shares the page or the receipts with can check them at
<https://scopeblind.com/verify> without an account or an upload.

## Trying it without a Monid balance

`GOVERNED_MONID_FIXTURE=1` makes the server answer discover, inspect, and run
from canned data, so the gate, the hold, the page, and the receipts can all be
exercised without spending anything. The fixture says so in every result.

## Limits

- The gate reads the ceiling the agent states, not Monid's bill. The server
  checks the ceiling against the published price and counts the reported cost;
  Monid's metering remains the truth.
- The per-run budget is enforced by this server's code, not by the standard.
  Keep the ledger file with the run.
- Endpoints, prices, and health come from Monid and change without notice.
- Nothing here changes what Monid records or charges.

See [references/REFERENCE.md](references/REFERENCE.md) for the tool schemas,
environment variables, receipt fields, and the exact refusal reasons.
