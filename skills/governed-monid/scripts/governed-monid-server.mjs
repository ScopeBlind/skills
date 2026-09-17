#!/usr/bin/env node
/**
 * governed-monid: an MCP server over the Monid CLI, built to run behind the
 * protect-mcp gate with a signed standard in force.
 *
 *   npx -y protect-mcp@0.24.1 --enforce --cedar ./policy --standard ./standard.json \
 *     --report '<the standard page's report URL>' -- node scripts/governed-monid-server.mjs
 *
 * Three tools. discover and inspect are free at Monid; run spends the user's
 * Monid balance, so run takes the highest price the agent accepts for the call
 * (amount_minor, US cents, currency USD). Two layers check that number:
 *   - the gate, from the signed standard: only the three named tools, at most
 *     the per-call limit, and a hold on the standard's page above the approval
 *     threshold, with a signed receipt for every call either way;
 *   - this server, from code the operator controls: the ceiling must cover the
 *     inspected price, and the run budget (GOVERNED_MONID_RUN_BUDGET_USD,
 *     default 5.00) is kept in a local ledger and refuses when exhausted.
 * Monid's own metering is the billing truth; the ledger is the agent's view.
 *
 * GOVERNED_MONID_FIXTURE=1 answers from canned data so the whole loop can be
 * tried, and tested, without a funded Monid key.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

const FIXTURE = process.env.GOVERNED_MONID_FIXTURE === '1';
const MONID = process.env.GOVERNED_MONID_CLI || 'monid';
const LEDGER = process.env.GOVERNED_MONID_LEDGER || '.governed-monid-ledger.json';
const BUDGET_MINOR = Math.round(Number(process.env.GOVERNED_MONID_RUN_BUDGET_USD || '5') * 100);

const TOOLS = [
  {
    name: 'monid_discover',
    description: 'Search the Monid catalog for endpoints that fit a job. Free at Monid. Returns candidates with price, health, and latency.',
    inputSchema: { type: 'object', properties: { query: { type: 'string', description: 'What you need, in plain words' }, limit: { type: 'integer', minimum: 1, maximum: 25 } }, required: ['query'] },
  },
  {
    name: 'monid_inspect',
    description: 'Read one endpoint: input schema, price, health, and typical run time. Free at Monid. Always inspect before run.',
    inputSchema: { type: 'object', properties: { provider: { type: 'string' }, endpoint: { type: 'string' } }, required: ['provider', 'endpoint'] },
  },
  {
    name: 'monid_run',
    description: 'Execute one Monid endpoint. Billable: this spends the user\'s Monid balance. State the highest price you accept for this call as amount_minor (US cents) with currency "USD"; take it from inspect. The gate checks it against the signed standard and may hold the call for a person; the server checks it against the inspected price and the run budget.',
    inputSchema: {
      type: 'object',
      properties: {
        provider: { type: 'string' }, endpoint: { type: 'string' },
        body: { type: 'object', description: 'Request body (inspect: input.body)' },
        query: { type: 'object', description: 'Query parameters (inspect: input.queryParams)' },
        path: { type: 'object', description: 'Path parameters (inspect: input.pathParams)' },
        amount_minor: { type: 'integer', minimum: 0, description: 'Highest price accepted for this call, in US cents' },
        currency: { type: 'string', enum: ['USD'] },
        wait: { type: 'boolean', description: 'Block until the run completes (default true)' },
      },
      required: ['provider', 'endpoint', 'amount_minor', 'currency'],
    },
  },
];

const FIXTURES = {
  discover: (query) => ({ query, results: [
    { provider: 'exa', endpoint: 'search', description: 'Neural web search with structured results', price: '$0.005 per call', health: 'healthy', p50_s: 1.2, p95_s: 2.9 },
    { provider: 'apify', endpoint: 'google-maps-scraper', description: 'Places, reviews, and contact details from Google Maps', price: '$0.02 per result', health: 'stable', p50_s: 24.0, p95_s: 61.0 },
  ] }),
  inspect: (provider, endpoint) => ({ provider, endpoint, price: provider === 'exa' ? '$0.005 per call' : '$0.02 per result', price_minor_estimate: provider === 'exa' ? 1 : 20, health: 'healthy', input: { body: { query: 'string', numResults: 'integer' }, queryParams: {}, pathParams: {} }, output: 'array of results' }),
  run: (provider, endpoint, body) => ({ run_id: `fixture-${Date.now().toString(36)}`, status: 'completed', provider, endpoint, cost_usd: provider === 'exa' ? 0.005 : 0.08, results: [{ title: 'Fixture result', url: 'https://example.invalid/fixture', note: `Answering ${JSON.stringify(body ?? {})} from canned data; no Monid balance was spent.` }] }),
};

function cli(args) {
  const r = spawnSync(MONID, args, { encoding: 'utf-8', env: { ...process.env, NO_COLOR: '1' }, maxBuffer: 16 * 1024 * 1024 });
  if (r.error) throw new Error(`${MONID} could not run (${r.error.message}). Install it with: npm install -g @monid-ai/cli@latest`);
  if (r.status !== 0) throw new Error(`${MONID} ${args[0]} failed (exit ${r.status}): ${(r.stderr || r.stdout || '').trim().slice(0, 800)}`);
  const text = (r.stdout || '').trim();
  try { return JSON.parse(text); } catch { return { text }; }
}

/** A price like "$0.005 per call" or "0.02/result" read into whole US cents, rounded up. Null when unreadable. */
function priceMinor(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number' && Number.isFinite(value)) return Math.ceil(value * 100);
  if (typeof value === 'string') { const m = /\$?\s*([0-9]+(?:\.[0-9]+)?)/.exec(value); if (m) return Math.ceil(Number(m[1]) * 100); }
  if (typeof value === 'object') for (const k of Object.keys(value)) if (/price|cost/i.test(k)) { const p = priceMinor(value[k]); if (p !== null) return p; }
  return null;
}
function findCost(obj) {
  if (!obj || typeof obj !== 'object') return null;
  for (const k of Object.keys(obj)) { if (/^(cost|cost_usd|total_cost|price_charged|charged)$/i.test(k)) { const p = priceMinor(obj[k]); if (p !== null) return p; } }
  for (const k of Object.keys(obj)) { const v = obj[k]; if (v && typeof v === 'object') { const p = findCost(v); if (p !== null) return p; } }
  return null;
}

function readLedger() {
  try { if (existsSync(LEDGER)) { const l = JSON.parse(readFileSync(LEDGER, 'utf-8')); if (l && l.version === 1 && Array.isArray(l.calls)) return l; } } catch { /* start fresh */ }
  return { version: 1, budget_minor: BUDGET_MINOR, spent_minor: 0, calls: [] };
}
function writeLedger(l) { writeFileSync(LEDGER, JSON.stringify(l, null, 2)); }

function discover(args) {
  const query = String(args.query || '').trim(); if (!query) throw new Error('query is required');
  const limit = Number.isInteger(args.limit) ? args.limit : 8;
  return FIXTURE ? FIXTURES.discover(query) : cli(['discover', '-q', query, '-l', String(limit), '-j']);
}
function inspect(args) {
  const { provider, endpoint } = args; if (!provider || !endpoint) throw new Error('provider and endpoint are required');
  return FIXTURE ? FIXTURES.inspect(provider, endpoint) : cli(['inspect', '-p', String(provider), '-e', String(endpoint), '-j']);
}
function run(args) {
  const { provider, endpoint } = args; if (!provider || !endpoint) throw new Error('provider and endpoint are required');
  if (args.currency !== 'USD') throw new Error('currency must be "USD"');
  if (!Number.isInteger(args.amount_minor) || args.amount_minor < 0) throw new Error('amount_minor must be a whole number of US cents');
  const ceiling = args.amount_minor;
  const ledger = readLedger();
  // The stated ceiling must cover the price Monid publishes for the endpoint.
  const inspected = inspect({ provider, endpoint });
  const price = Number.isInteger(inspected.price_minor_estimate) ? inspected.price_minor_estimate : priceMinor(inspected.price ?? inspected);
  if (price !== null && price > ceiling) return { refused: 'ceiling_below_price', detail: `The endpoint's published price is ${price} cents per unit; you stated a ceiling of ${ceiling}. Inspect it and restate the ceiling honestly.`, inspected_price_minor: price };
  if (ledger.spent_minor + ceiling > ledger.budget_minor) return { refused: 'run_budget_exhausted', detail: `This run's budget is ${ledger.budget_minor} cents; ${ledger.spent_minor} spent, ${ceiling} requested. Ask the person for a new budget or a new run.`, budget_minor: ledger.budget_minor, spent_minor: ledger.spent_minor };
  let result;
  if (FIXTURE) result = FIXTURES.run(provider, endpoint, args.body);
  else {
    const cliArgs = ['run', '-p', String(provider), '-e', String(endpoint)];
    if (args.body) cliArgs.push('-i', JSON.stringify(args.body));
    if (args.query) cliArgs.push('--query', JSON.stringify(args.query));
    if (args.path) cliArgs.push('--path', JSON.stringify(args.path));
    if (args.wait !== false) cliArgs.push('-w');
    result = cli(cliArgs);
  }
  const settled = findCost(result);
  const charged = settled === null ? ceiling : Math.min(settled, ceiling);
  ledger.spent_minor += charged;
  ledger.calls.push({ at: new Date().toISOString(), provider, endpoint, ceiling_minor: ceiling, settled_minor: settled, counted_minor: charged, fixture: FIXTURE });
  writeLedger(ledger);
  return { result, ceiling_minor: ceiling, settled_minor: settled, counted_minor: charged, remaining_budget_minor: ledger.budget_minor - ledger.spent_minor, note: settled === null ? 'Monid did not report a cost in the result; the ceiling was counted against the budget. Monid\'s own metering is the billing truth.' : 'Counted against the run budget from the cost Monid reported.' };
}

const HANDLERS = { monid_discover: discover, monid_inspect: inspect, monid_run: run };
const respond = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
const fail = (id, code, message) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }) + '\n');

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let req; try { req = JSON.parse(line); } catch { return; }
  const { id, method, params } = req;
  if (method === 'initialize') return respond(id, { protocolVersion: params?.protocolVersion || '2024-11-05', serverInfo: { name: 'governed-monid', version: '0.1.0' }, capabilities: { tools: {} } });
  if (method === 'notifications/initialized' || method === 'ping') return id === undefined ? undefined : respond(id, {});
  if (method === 'tools/list') return respond(id, { tools: TOOLS });
  if (method === 'tools/call') {
    const name = params?.name; const handler = HANDLERS[name];
    if (!handler) return respond(id, { content: [{ type: 'text', text: `Unknown tool: ${name}` }], isError: true });
    try { const out = handler(params?.arguments || {}); return respond(id, { content: [{ type: 'text', text: JSON.stringify(out, null, 2) }], isError: !!out?.refused }); }
    catch (e) { return respond(id, { content: [{ type: 'text', text: e instanceof Error ? e.message : String(e) }], isError: true }); }
  }
  if (id !== undefined) fail(id, -32601, `Method not found: ${method}`);
});
