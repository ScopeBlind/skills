---
name: put-a-standard-in-force
description: Write the rules for an agent's work in plain English on scopeblind.com, sign them in the browser, and run any MCP server or Claude Code session behind the protect-mcp gate so the rules are enforced before each call and every call leaves a signed receipt. Use when a person wants to limit what an agent may do (tools, per-call amounts, approvals above a threshold, rules over the history of calls) and keep a checkable record.
license: MIT
compatibility: Node.js 20 or newer; a browser for writing and signing the standard
metadata:
  author: ScopeBlind
  version: "0.1.0"
---

# Put a standard in force

A standard is a short signed document that says what an agent may do: which
tools, how much per call, what needs a named person's approval, and which rules
hold over the whole history of calls. The gate compiles it to policy and
enforces it before a call runs. Every call, allowed or refused, leaves a signed,
chained receipt.

## Steps

1. **Write it.** Open <https://scopeblind.com/write> and describe the work in
   sentences: "The work is paying vendor invoices for a client. Only
   pay_invoice and send_email. At most USD 2,000 per payment. A person approves
   above USD 500. Never send_email after reading a secret." The page reads each
   sentence back as an exact term and marks where each one is checked: at the
   gate, over the history, on the records, or by the environment. Anything the
   page could not interpret is listed; do not assume it is enforced.

2. **Rehearse it.** On the Rehearse tab, load a recorded call log and see what
   the standard would allow, refuse, or hold, before signing.

3. **Sign it.** On the Sign tab, the person signs with a key generated in their
   browser. Download `standard.json` and `policy/standard.cedar`. A signature
   covers one exact standard; change anything and sign again.

4. **Publish its page** (optional, needed for approvals). "Publish this
   standard's page" gives a link where the signer decides held actions, a
   report URL, and a write token shown once. Put the token in the environment
   where the gate runs, as `PROTECT_MCP_REPORT_TOKEN`, and nowhere else.

5. **Run the gate.** In a working folder holding `standard.json` and
   `policy/`, create the signing key once, then launch your MCP server through
   the gate:

   ```bash
   npx -y protect-mcp@0.24.1 init
   npx -y protect-mcp@0.24.1 --enforce --cedar ./policy --standard ./standard.json \
     --report 'https://scopeblind.com/api/standard?s=<standard id>' \
     -- <your MCP server command>
   ```

   For a coding agent that calls tools through Claude Code hooks instead of an
   MCP server, install the hooks once and serve the policy:

   ```bash
   npx -y protect-mcp@0.24.1 init-hooks
   npx -y protect-mcp@0.24.1 serve --enforce --cedar ./policy --standard ./standard.json \
     --report 'https://scopeblind.com/api/standard?s=<standard id>'
   ```

## What the gate does with a call

- A tool the standard does not name is refused before it runs.
- A call whose `amount_minor` and `currency` are over the per-call limit, or in
  another currency, is refused before it runs.
- A call above the approval threshold is held. The agent receives
  `REQUIRES_APPROVAL` naming the page. The signer decides there; a retry of the
  same exact call then proceeds with the decision in its receipt, or is
  refused. A changed call is a new action.
- Every decision is appended to `.protect-mcp-receipts.jsonl`, chained to the
  previous receipt, and posted to the page after it is written locally.
  Reporting never blocks a call.

## Honest limits

- The gate enforces calls that pass through it. Routes an operator did not put
  behind the gate are not covered, and the record says so.
- Amounts are read from the call's `amount_minor` (integer cents) or `amount`
  plus `currency`. A call with no amount is not a payment.
- Rules over history are replayed at every receipt inside the gate's run.
- The page stores the standard and the receipts sent to it. Verification of a
  downloaded record runs in the reader's browser.
