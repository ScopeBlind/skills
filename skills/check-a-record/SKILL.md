---
name: check-a-record
description: Check a record someone sent you from ScopeBlind or protect-mcp (a signed standard, a receipt log, a shared invoice job, a repository review, a verified run) without uploading it, in the browser at scopeblind.com/verify or with the open @veritasacta/verify command. Use when a person receives a JSON or JSONL record and wants to know what it establishes, what it does not, and whether it has been altered.
license: MIT
compatibility: A browser, or Node.js 20 or newer for the command line
metadata:
  author: ScopeBlind
  version: "0.1.0"
---

# Check a record

A record is plain JSON. Checking it recomputes every digest and signature,
replays the standard's rules where there is one, and says four things
separately: whether the record is intact, which keys authorized it, what the
destination confirmed, and whether the recipient accepted it.

## In the browser

Open <https://scopeblind.com/verify> and drop the file, or a run's files
together. The check runs in the browser; the file is not uploaded. The page
lists what was checked, what it establishes, and what it does not.

Paste a key you received through a channel you trust to check authority
independently. Leaving it empty checks consistency with the key named inside
the record, which proves the file agrees with itself, not who holds the key.

## On the command line

```bash
npx @veritasacta/verify standard.json
npx @veritasacta/verify manifest.json --standard standard.json --receipts receipts.jsonl --calls calls.jsonl
```

The verifier is Apache-2.0, contacts no server, and prints `NOT CHECKED` with a
code when a result cannot be decided, `INVALID` only when a check ran and
failed. Its conformance vectors are public at
<https://github.com/ScopeBlind/agent-governance-testvectors>.

## Reading the answer

- **Integrity** holds when every signature and chain link checks out.
- **Authority** names the keys, and whether the standard accepts them.
- **Observed effect** is what a receiver or a sandbox ledger signed; it is not
  proof of a real-world payment or deployment unless the receiver is one you
  trust.
- **Recipient** is whether someone signed an acceptance of this exact record.

A valid signature proves the bytes were signed by a key. It does not prove the
inputs were true, that every action passed through the gate, or a person's
legal identity. The page and the command say so; repeat it when you report.
