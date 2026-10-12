// Run: node test.mjs
// Builds a test copy of worker.js (stubbing cloudflare:email), runs it against a real SQLite
// database with the real schema, and signs Stripe events exactly the way Stripe does.
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const here = new URL('.', import.meta.url).pathname;
mkdirSync(`${here}.test`, { recursive: true });
const src = readFileSync(`${here}worker.js`, 'utf8')
  .replace("import { EmailMessage } from 'cloudflare:email';",
    'class EmailMessage { constructor(from, to, raw) { this.from = from; this.to = to; this.raw = raw; } }')
  + '\nexport const _test = { verifyStripe, claimEvent, dailyCheck, STALE_CLAIM_MS, NUDGE_AFTER_MS };\n';
writeFileSync(`${here}.test/worker.mjs`, src);
const { default: W, _test } = await import(`${here}.test/worker.mjs`);

// ---- fakes ---------------------------------------------------------------
function makeD1() {
  const sql = new DatabaseSync(':memory:');
  sql.exec(readFileSync(`${here}schema.sql`, 'utf8'));
  const stmt = (q, args = []) => ({
    bind: (...a) => stmt(q, a),
    run: async () => { const r = sql.prepare(q).run(...args); return { meta: { changes: Number(r.changes) } }; },
    first: async () => sql.prepare(q).get(...args) ?? null,
    all: async () => ({ results: sql.prepare(q).all(...args) }),
  });
  return { prepare: (q) => stmt(q), batch: async (ss) => Promise.all(ss.map((s) => s.run())), sql };
}

const SECRET = 'whsec_test';
function makeEnv() {
  const sent = [];
  const env = {
    DB: makeD1(), STRIPE_WEBHOOK_SECRET: SECRET,
    SLACK_CLIENT_ID: '1.2', SLACK_CLIENT_SECRET: 'slacksecret',
    MAILER: { fail: false, send: async (m) => { if (env.MAILER.fail) throw new Error('mail down'); sent.push(m.raw); } },
  };
  return { env, sent };
}

const sign = (body, secret = SECRET, t = Math.floor(Date.now() / 1000)) =>
  `t=${t},v1=${createHmac('sha256', secret).update(`${t}.${body}`).digest('hex')}`;

function stripeReq(event, { secret = SECRET, t, body } = {}) {
  const raw = body ?? JSON.stringify(event);
  return new Request('https://cronussignals.com/api/stripe/webhook', {
    method: 'POST', body: raw, headers: { 'stripe-signature': sign(JSON.stringify(event), secret, t), 'content-type': 'application/json' },
  });
}
const post = async (env, event, opts) => W.fetch(stripeReq(event, opts), env);

const checkout = (id = 'evt_1', sid = 'cs_1', extra = {}) => ({
  id, type: 'checkout.session.completed', data: { object: {
    id: sid, object: 'checkout.session', payment_status: 'paid', customer: 'cus_1', subscription: 'sub_1',
    amount_total: 9900, currency: 'usd', metadata: { plan: 'pro_agency' },
    customer_details: { email: 'ana@agency.co', name: 'Ana Ruiz' }, ...extra,
  } },
});
const subDeleted = (id = 'evt_del', sub = 'sub_1') => ({
  id, type: 'customer.subscription.deleted',
  data: { object: { id: sub, customer: 'cus_1', cancellation_details: { reason: 'cancellation_requested', feedback: 'too_expensive' } } },
});
const header = (raw, name) => raw.split('\r\n\r\n')[0].split('\r\n').find((l) => l.startsWith(`${name}: `))?.slice(name.length + 2);
const clients = (env) => env.DB.sql.prepare('select * from clients').all();

let n = 0;
const test = async (name, fn) => { await fn(); n++; console.log('  ok', name); };

// ---- signature -----------------------------------------------------------
await test('rejects wrong secret', async () => {
  const { env } = makeEnv();
  assert.equal((await post(env, checkout(), { secret: 'whsec_wrong' })).status, 400);
  assert.equal(clients(env).length, 0);
});
await test('rejects body that differs from what was signed (re-serialized)', async () => {
  const { env } = makeEnv();
  const ev = checkout();
  assert.equal((await post(env, ev, { body: JSON.stringify(ev, null, 2) })).status, 400);
});
await test('rejects old timestamp (replay)', async () => {
  const { env } = makeEnv();
  assert.equal((await post(env, checkout(), { t: Math.floor(Date.now() / 1000) - 600 })).status, 400);
});
await test('rejects missing header and GET', async () => {
  const { env } = makeEnv();
  const r = await W.fetch(new Request('https://cronussignals.com/api/stripe/webhook', { method: 'POST', body: '{}' }), env);
  assert.equal(r.status, 400);
  assert.equal((await W.fetch(new Request('https://cronussignals.com/api/stripe/webhook'), env)).status, 405);
});
await test('accepts any of several v1 signatures (secret rotation)', async () => {
  const t = Math.floor(Date.now() / 1000);
  const body = new TextEncoder().encode('{"a":1}');
  const good = createHmac('sha256', SECRET).update(`${t}.{"a":1}`).digest('hex');
  assert.equal(await _test.verifyStripe(body.buffer, `t=${t},v1=deadbeef,v1=${good}`, SECRET), true);
});
await test('handles non-ASCII names byte-exactly', async () => {
  const { env, sent } = makeEnv();
  const ev = checkout('evt_u', 'cs_u', { customer_details: { email: 'jo@x.co', name: 'José Müller 🚀' } });
  assert.equal((await post(env, ev)).status, 200);
  assert.ok(header(sent[0], 'Subject').startsWith('=?UTF-8?B?'));
});

// ---- provisioning --------------------------------------------------------
await test('new client: row stored, one email to inbox with reply-to client', async () => {
  const { env, sent } = makeEnv();
  assert.equal((await post(env, checkout())).status, 200);
  const [c] = clients(env);
  assert.equal(c.email, 'ana@agency.co');
  assert.equal(c.subscription_id, 'sub_1');
  assert.ok(c.notified_at);
  assert.equal(sent.length, 1);
  assert.equal(header(sent[0], 'Subject'), 'New Pro Agency client: Ana Ruiz');
  assert.equal(header(sent[0], 'Reply-To'), 'ana@agency.co');
  assert.equal(header(sent[0], 'To'), 'cronussignals@gmail.com');
  assert.match(sent[0], /99\.00 USD/);
  assert.match(sent[0], /dashboard\.stripe\.com\/customers\/cus_1/);
});
await test('same event twice: one row, one email', async () => {
  const { env, sent } = makeEnv();
  await post(env, checkout());
  const r = await post(env, checkout());
  assert.deepEqual(await r.json(), { received: true, duplicate: true });
  assert.equal(clients(env).length, 1);
  assert.equal(sent.length, 1);
});
await test('different event, same session (async_payment_succeeded): still one email', async () => {
  const { env, sent } = makeEnv();
  await post(env, checkout('evt_1'));
  await post(env, { ...checkout('evt_2'), type: 'checkout.session.async_payment_succeeded' });
  assert.equal(clients(env).length, 1);
  assert.equal(sent.length, 1);
});
await test('unpaid session (bank transfer pending): nothing yet', async () => {
  const { env, sent } = makeEnv();
  assert.equal((await post(env, checkout('evt_1', 'cs_1', { payment_status: 'unpaid' }))).status, 200);
  assert.equal(clients(env).length, 0);
  assert.equal(sent.length, 0);
});
await test('email fails: 500, event released, retry sends exactly once', async () => {
  const { env, sent } = makeEnv();
  env.MAILER.fail = true;
  assert.equal((await post(env, checkout())).status, 500);
  assert.equal(env.DB.sql.prepare('select count(*) n from stripe_events').get().n, 0);
  assert.equal(clients(env)[0].notified_at, null);
  env.MAILER.fail = false;
  assert.equal((await post(env, checkout())).status, 200);
  assert.equal(clients(env).length, 1);
  assert.equal(sent.length, 1);
});
await test('event in flight elsewhere: 409 so Stripe retries later', async () => {
  const { env } = makeEnv();
  env.DB.sql.prepare("insert into stripe_events (event_id, type, received_at) values ('evt_1', 'x', ?)").run(Date.now());
  assert.equal((await post(env, checkout())).status, 409);
});
await test('stale claim (crashed attempt) is taken over', async () => {
  const { env, sent } = makeEnv();
  env.DB.sql.prepare("insert into stripe_events (event_id, type, received_at) values ('evt_1', 'x', ?)").run(Date.now() - _test.STALE_CLAIM_MS - 1000);
  assert.equal((await post(env, checkout())).status, 200);
  const ev = env.DB.sql.prepare('select * from stripe_events').get();
  assert.equal(ev.status, 'done');
  assert.equal(ev.attempts, 2);
  assert.equal(sent.length, 1);
});
await test('missing DB binding: 500 (Stripe keeps retrying), nothing lost', async () => {
  const { env } = makeEnv();
  delete env.DB;
  assert.equal((await post(env, checkout())).status, 500);
});
await test('other event types are acknowledged and ignored', async () => {
  const { env, sent } = makeEnv();
  const r = await post(env, { id: 'evt_x', type: 'invoice.paid', data: { object: {} } });
  assert.equal((await r.json()).ignored, 'invoice.paid');
  assert.equal(sent.length, 0);
});

// ---- cancellation & failures ---------------------------------------------
await test('cancellation: marked cancelled, one "remove their endpoints" email', async () => {
  const { env, sent } = makeEnv();
  await post(env, checkout());
  assert.equal((await post(env, subDeleted())).status, 200);
  await post(env, subDeleted('evt_del_dup'));            // a second, different event for the same sub
  const [c] = clients(env);
  assert.equal(c.status, 'cancelled');
  assert.equal(sent.length, 2);
  assert.match(header(sent[1], 'Subject'), /^Cancelled: Ana Ruiz/);
  assert.match(sent[1], /ENDPOINTS_JSON/);
  assert.match(sent[1], /too_expensive/);
});
await test('cancellation for unknown subscription still tells you', async () => {
  const { env, sent } = makeEnv();
  await post(env, subDeleted('evt_d', 'sub_unknown'));
  assert.equal(sent.length, 1);
  assert.match(header(sent[0], 'Subject'), /no matching client/);
});
await test('renewal payment failure: heads-up email; first-payment failure ignored', async () => {
  const { env, sent } = makeEnv();
  const inv = { amount_due: 9900, currency: 'usd', customer_email: 'ana@agency.co', customer_name: 'Ana Ruiz', attempt_count: 1 };
  await post(env, { id: 'evt_f1', type: 'invoice.payment_failed', data: { object: { ...inv, billing_reason: 'subscription_cycle' } } });
  await post(env, { id: 'evt_f2', type: 'invoice.payment_failed', data: { object: { ...inv, billing_reason: 'subscription_create' } } });
  assert.equal(sent.length, 1);
  assert.match(header(sent[0], 'Subject'), /^Payment failed: Ana Ruiz/);
});

// ---- endpoint list & daily check ------------------------------------------
const endpointsForm = (email) => new Request('https://cronussignals.com/api/endpoints', {
  method: 'POST', headers: { origin: 'https://cronussignals.com', 'content-type': 'application/json' },
  body: JSON.stringify({ name: 'Ana', email, agency: 'Agency Co', elapsed: 9000,
    rows: [{ client: 'Shop', label: 'Orders webhook', url: 'https://shop.co/hook', expect: '200' }] }),
});

await test('endpoint list from the paying email marks the client set up (case-insensitive)', async () => {
  const { env, sent } = makeEnv();
  await post(env, checkout());
  assert.equal((await W.fetch(endpointsForm('ANA@agency.co'), env)).status, 200);
  assert.ok(clients(env)[0].endpoints_received_at);
  assert.match(header(sent[1], 'Subject'), /^Endpoint list: Agency Co/);
});
await test('endpoint list sent before the payment event: new-client email says it already arrived', async () => {
  const { env, sent } = makeEnv();
  env.DB.sql.prepare("insert into clients (session_id, email, created_at, endpoints_received_at, notified_at) values ('cs_old', 'ana@agency.co', 1, 1, 1)").run();
  await post(env, checkout());
  assert.match(sent[0], /has already arrived/);
});
await test('daily check: reminds once about clients with no list after 48h', async () => {
  const { env, sent } = makeEnv();
  await post(env, checkout('evt_a', 'cs_a'));
  await post(env, checkout('evt_b', 'cs_b', { subscription: 'sub_b', customer_details: { email: 'bo@b.co', name: 'Bo' } }));
  await W.fetch(endpointsForm('bo@b.co'), env);
  const later = Date.now() + _test.NUDGE_AFTER_MS + 60_000;
  assert.deepEqual(await _test.dailyCheck(env, Date.now()), { nudged: 0 });       // too early
  assert.deepEqual(await _test.dailyCheck(env, later), { nudged: 1 });
  assert.deepEqual(await _test.dailyCheck(env, later), { nudged: 0 });            // only once
  const reminder = sent.at(-1);
  assert.match(header(reminder, 'Subject'), /^1 paid client with no endpoint list yet/);
  assert.match(reminder, /ana@agency\.co/);
  assert.doesNotMatch(reminder, /bo@b\.co/);
});
await test('scheduled() entry point runs the daily check', async () => {
  const { env } = makeEnv();
  let p;
  await W.scheduled({}, env, { waitUntil: (x) => { p = x; } });
  await p;
});

// ---- existing routes unchanged --------------------------------------------
await test('pilot form still works and still rejects bad input', async () => {
  const { env, sent } = makeEnv();
  const ok = await W.fetch(new Request('https://cronussignals.com/api/pilot', { method: 'POST',
    headers: { origin: 'https://cronussignals.com', 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'A', email: 'a@b.co', agency: 'X', url1: 'https://x.co', elapsed: 9000 }) }), env);
  assert.equal(ok.status, 200);
  assert.match(header(sent[0], 'Subject'), /^Pilot request: X/);
  const bad = await W.fetch(new Request('https://cronussignals.com/api/pilot', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: '', elapsed: 9000 }) }), env);
  assert.equal(bad.status, 400);
});
await test('endpoints form works without a DB binding (bookkeeping never blocks it)', async () => {
  const { env } = makeEnv();
  delete env.DB;
  assert.equal((await W.fetch(endpointsForm('a@b.co'), env)).status, 200);
});
await test('Slack install still redirects to Slack; other paths 404', async () => {
  const { env } = makeEnv();
  const r = await W.fetch(new Request('https://cronussignals.com/api/slack/install'), env);
  assert.equal(r.status, 302);
  assert.match(r.headers.get('location'), /^https:\/\/slack\.com\/oauth\/v2\/authorize\?.*scope=incoming-webhook/);
  assert.equal((await W.fetch(new Request('https://cronussignals.com/api/nope'), env)).status, 404);
});

await test('heartbeats: registered ids record check-ins, unknown ids are refused', async () => {
  const { env } = makeEnv();
  env.HEARTBEAT_READ_TOKEN = 'readtok';
  const id = 'hb_' + 'a1'.repeat(12);
  env.DB.sql.prepare('insert into heartbeats (id, label, created_at) values (?, ?, ?)').run(id, 'acme / orders zap', 1);
  const ping = (hid, method = 'POST') => W.fetch(new Request(`https://cronussignals.com/api/hb/${hid}`, { method }), env);
  const status = (hid, tok = 'readtok') => W.fetch(new Request(`https://cronussignals.com/api/hb/${hid}/status`,
    { headers: tok ? { authorization: `Bearer ${tok}` } : {} }), env);

  let s = await (await status(id)).json();
  assert.equal(s.last_ping, null);
  assert.equal((await ping(id)).status, 200);
  assert.equal((await ping(id, 'GET')).status, 200);  // within 30 s: accepted, counted once
  s = await (await status(id)).json();
  assert.ok(s.last_ping > 0);
  assert.equal(s.pings, 1);

  assert.equal((await ping('hb_' + 'b2'.repeat(12))).status, 404);   // not registered
  assert.equal((await ping('not-an-id')).status, 404);
  assert.equal((await ping(id, 'DELETE')).status, 405);
  assert.equal((await status(id, 'wrong')).status, 401);
  assert.equal((await status(id, null)).status, 401);
  delete env.HEARTBEAT_READ_TOKEN;
  assert.equal((await status(id, 'readtok')).status, 401);           // no token configured: closed
});

console.log(`\nall ${n} checks passed`);
