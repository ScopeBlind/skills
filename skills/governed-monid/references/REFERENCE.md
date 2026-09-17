# governed-monid reference

## Tools

| Tool | Arguments | Billable |
|---|---|---|
| `monid_discover` | `query` (string), `limit` (1 to 25) | No |
| `monid_inspect` | `provider`, `endpoint` | No |
| `monid_run` | `provider`, `endpoint`, `body`, `query`, `path`, `amount_minor` (US cents), `currency` ("USD"), `wait` | Yes |

The server shells out to the `monid` CLI (`discover -q -l -j`, `inspect -p -e -j`,
`run -p -e -i --query --path -w`) using the keys the user configured with
`monid keys add`. The server never sees or stores the key.

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `GOVERNED_MONID_RUN_BUDGET_USD` | Per-run budget counted in the local ledger | `5` |
| `GOVERNED_MONID_LEDGER` | Ledger file path | `.governed-monid-ledger.json` |
| `GOVERNED_MONID_CLI` | CLI executable | `monid` |
| `GOVERNED_MONID_FIXTURE` | `1` answers from canned data; nothing is spent | unset |
| `PROTECT_MCP_REPORT_TOKEN` | The standard page's write token, read by the gate | unset |

## Decisions and reasons

Gate (from the signed standard; recorded in the receipt's `reason`):

| Reason | Meaning |
|---|---|
| `cedar_allow` | The tool is named in the standard and the ceiling is within the limit |
| `standard_tool_not_allowed` | The tool is not one the standard names |
| `standard_amount_over_limit` | The ceiling is above the per-call limit |
| `standard_currency_not_permitted` | The currency is not the standard's |
| `standard_requires_person` | Held: the ceiling is above the approval threshold; posted to the page |
| `person_denied` | A person denied this exact action on the page |
| `cedar_allow` with `approval` | A person approved this exact action; the receipt carries their decision |

Server (returned as a tool result with `isError: true`):

| Reason | Meaning |
|---|---|
| `ceiling_below_price` | The stated ceiling is below the endpoint's published price |
| `run_budget_exhausted` | Spent plus the ceiling would exceed the run budget |

## Receipt fields that matter here

`tool_name`, `decision`, `reason`, `policy_digest` (the compiled standard),
`standard.request_id` and `standard.digest`, `action_readback.payload_hash`
(the exact call), `approval.hid` and `approval.approver_key_id` when a person
decided, `previousReceiptHash` (the chain).

## The bundled sample standard

`assets/standard.json` was written on scopeblind.com/write from these
sentences and signed with a throwaway sample key:

> The work is retrieving data through Monid for a research task. Only
> monid_discover, monid_inspect, monid_run. At most USD 1 per call. A person
> approves above USD 0.25.

Write and sign your own for real work; the sample's signer is nobody.
