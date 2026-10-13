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
  + '\nexport const _test = { verifyStripe, claimEvent, dailyCheck, signLink, STALE_CLAIM_MS, NUDGE_AFTER_MS };\n';
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

// ---- clients in D1: forms save pending rows, signed links activate them --------
const BASE = 'https://cronussignals.com';
const actLinks = (raw) => raw.match(/https:\/\/cronussignals\.com\/api\/admin\/act\?t=\S+/g) ?? [];
const rowsOf = (env) => env.DB.sql.prepare('select * from endpoints order by client_id, endpoint_name').all();
function clientEnv() {
  const e = makeEnv();
  Object.assign(e.env, { ADMIN_LINK_SECRET: 'linksecret', HEARTBEAT_READ_TOKEN: 'readtok', TELEGRAM_WEBHOOK_SECRET: 'tgsecret' });
  return e;
}
const pilotForm = (agency = 'Bright Pixel', urls = ['https://shop.co/api/health', 'https://shop.co/api/health']) =>
  new Request(`${BASE}/api/pilot`, { method: 'POST', headers: { origin: BASE, 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'Bo', email: 'bo@bright.co', agency, url1: urls[0], url2: urls[1], elapsed: 9000 }) });
const tgUpdate = (body, secret = 'tgsecret') => new Request(`${BASE}/api/telegram/webhook`, {
  method: 'POST', headers: { 'content-type': 'application/json', 'x-telegram-bot-api-secret-token': secret }, body: JSON.stringify(body) });
const joined = (id = -100555, title = 'Bright Pixel alerts') => ({ my_chat_member: {
  chat: { id, title, type: 'supergroup' }, from: { first_name: 'Bo' },
  old_chat_member: { status: 'left' }, new_chat_member: { status: 'member' } } });
const sentinelRead = async (env, tok = 'readtok') => W.fetch(new Request(`${BASE}/api/sentinel/endpoints`,
  { headers: tok ? { authorization: `Bearer ${tok}` } : {} }), env);
const act = (link, method = 'GET') => {
  if (method === 'GET') return new Request(link);
  const t = new URL(link).searchParams.get('t');
  return new Request(`${BASE}/api/admin/act`, { method: 'POST', body: new URLSearchParams({ t }),
    headers: { 'content-type': 'application/x-www-form-urlencoded' } });
};

await test('pilot form saves its URLs as pending; nothing is monitored yet', async () => {
  const { env, sent } = clientEnv();
  assert.equal((await W.fetch(pilotForm(), env)).status, 200);
  const rows = rowsOf(env);
  assert.deepEqual(rows.map((r) => [r.client_id, r.endpoint_name, r.status, r.source]), [
    ['bright-pixel--pilot', 'shop.co/api/health', 'pending', 'pilot'],
    ['bright-pixel--pilot', 'shop.co/api/health (2)', 'pending', 'pilot'],
  ]);
  assert.match(sent[0], /Saved as pending for client_id bright-pixel--pilot: 2 endpoints/);
  assert.match(sent[0], /When they add @cronus_alerts_bot or connect Slack/);
  assert.deepEqual((await (await sentinelRead(env)).json()).endpoints, []);
});

await test('Telegram connected email links pending clients; GET only previews, POST activates', async () => {
  const { env, sent } = clientEnv();
  await W.fetch(endpointsForm('ana@agency.co'), env);
  assert.equal((await W.fetch(tgUpdate(joined()), env)).status, 200);
  const mail = sent.at(-1);
  assert.match(header(mail, 'Subject'), /^Telegram connected/);
  assert.match(mail, /agency-co--shop {2}\(1 endpoint, Agency Co, ana@agency\.co\)/);
  const [link] = actLinks(mail);
  assert.ok(link);

  const preview = await W.fetch(act(link), env);
  assert.equal(preview.status, 200);
  const html = await preview.text();
  assert.match(html, /https:\/\/shop\.co\/hook/);
  assert.match(html, /Telegram group &quot;Bright Pixel alerts&quot;/);
  assert.match(preview.headers.get('content-security-policy'), /default-src 'none'/);
  assert.equal(rowsOf(env)[0].status, 'pending');            // a mail scanner's GET changes nothing

  const done = await W.fetch(act(link, 'POST'), env);
  assert.equal(done.status, 200);
  assert.match(await done.text(), /1 endpoint active[\s\S]*test_client/);
  assert.equal(rowsOf(env)[0].status, 'active');
  const list = (await (await sentinelRead(env)).json()).endpoints;
  assert.deepEqual(list, [{ expected_status: [200], client_id: 'agency-co--shop', endpoint_name: 'Orders webhook',
    target_url: 'https://shop.co/hook', telegram_chat_id: '-100555' }]);
});

await test('endpoint list arriving after Telegram links to the unused chat', async () => {
  const { env, sent } = clientEnv();
  await W.fetch(tgUpdate(joined()), env);
  assert.match(sent[0], /No endpoint lists are waiting/);
  await W.fetch(endpointsForm('ana@agency.co'), env);
  const mail = sent.at(-1);
  assert.match(mail, /agency-co--shop -> Telegram group "Bright Pixel alerts"/);
  await W.fetch(act(actLinks(mail)[0], 'POST'), env);
  assert.equal(rowsOf(env)[0].telegram_chat_id, '-100555');
  // Once in use, the chat is no longer offered to the next list.
  await W.fetch(pilotForm('Other Agency', ['https://o.co/h', '']), env);
  assert.equal(actLinks(sent.at(-1)).length, 0);
});

await test('a public form can never change an active endpoint', async () => {
  const { env, sent } = clientEnv();
  await W.fetch(endpointsForm('ana@agency.co'), env);
  await W.fetch(tgUpdate(joined()), env);
  await W.fetch(act(actLinks(sent.at(-1))[0], 'POST'), env);
  const evil = new Request(`${BASE}/api/endpoints`, { method: 'POST', headers: { origin: BASE, 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'X', email: 'x@evil.co', agency: 'Agency Co', elapsed: 9000,
      rows: [{ client: 'Shop', label: 'Orders webhook', url: 'https://evil.co/', expect: '' }] }) });
  assert.equal((await W.fetch(evil, env)).status, 200);
  const [r] = rowsOf(env);
  assert.equal(r.target_url, 'https://shop.co/hook');
  assert.equal(r.contact_email, 'ana@agency.co');
  assert.match(sent.at(-1), /NOT changed by this form[\s\S]*agency-co--shop \/ Orders webhook/);
});

await test('tampered, expired or unsigned links do nothing', async () => {
  const { env, sent } = clientEnv();
  await W.fetch(endpointsForm('ana@agency.co'), env);
  await W.fetch(tgUpdate(joined()), env);
  const link = actLinks(sent.at(-1))[0];
  const t = new URL(link).searchParams.get('t');
  const forged = `${BASE}/api/admin/act?t=${t.split('.')[0]}.AAAA`;
  assert.equal((await W.fetch(act(forged, 'POST'), env)).status, 400);
  const expired = await _test.signLink(env, { a: 'assign', c: 'agency-co--shop', d: 'tg:-100555', e: Date.now() - 1 });
  assert.equal((await W.fetch(act(expired, 'POST'), env)).status, 400);
  const wrongKey = await _test.signLink({ ADMIN_LINK_SECRET: 'other' }, { a: 'assign', c: 'agency-co--shop', d: 'tg:-100555', e: Date.now() + 9e6 });
  assert.equal((await W.fetch(act(wrongKey, 'POST'), env)).status, 400);
  const unknown = await _test.signLink(env, { a: 'delete_all', c: 'agency-co--shop', e: Date.now() + 9e6 });
  assert.equal((await W.fetch(act(unknown, 'POST'), env)).status, 400);
  assert.equal(rowsOf(env)[0].status, 'pending');
  delete env.ADMIN_LINK_SECRET;
  assert.equal((await W.fetch(act(link, 'POST'), env)).status, 400);
  await W.fetch(tgUpdate(joined(-100777, 'Second')), env);
  assert.match(sent.at(-1), /One-click links are off/);
});

await test('a chat the bot was removed from cannot be assigned', async () => {
  const { env, sent } = clientEnv();
  await W.fetch(endpointsForm('ana@agency.co'), env);
  await W.fetch(tgUpdate(joined()), env);
  const link = actLinks(sent.at(-1))[0];
  const gone = joined(); gone.my_chat_member.old_chat_member.status = 'member'; gone.my_chat_member.new_chat_member.status = 'kicked';
  await W.fetch(tgUpdate(gone), env);
  assert.equal((await W.fetch(act(link, 'POST'), env)).status, 409);
  assert.equal(rowsOf(env)[0].status, 'pending');
});

await test('pause and resume from the result page; cancellation email pauses by email', async () => {
  const { env, sent } = clientEnv();
  await post(env, checkout());
  await W.fetch(endpointsForm('ana@agency.co'), env);
  await W.fetch(tgUpdate(joined()), env);
  const done = await (await W.fetch(act(actLinks(sent.at(-1))[0], 'POST'), env)).text();
  const pause = done.match(/href="([^"]+)">pause later/)[1].replace(/&amp;/g, '&');
  await W.fetch(act(pause, 'POST'), env);
  assert.equal(rowsOf(env)[0].status, 'paused');
  assert.deepEqual((await (await sentinelRead(env)).json()).endpoints, []);
  const resumePage = await (await W.fetch(act(pause, 'POST'), env)).text();   // already paused
  assert.match(resumePage, /Nothing to do/);
  // resume, then cancel the subscription
  const resume = await _test.signLink(env, { a: 'resume', c: 'agency-co--shop', e: Date.now() + 9e6 });
  await W.fetch(act(resume, 'POST'), env);
  assert.equal(rowsOf(env)[0].status, 'active');
  await post(env, subDeleted());
  const cancelMail = sent.at(-1);
  assert.match(header(cancelMail, 'Subject'), /^Cancelled/);
  const [stop] = actLinks(cancelMail);
  assert.match(await (await W.fetch(act(stop), env)).text(), /every endpoint submitted from <b>ana@agency\.co/);
  await W.fetch(act(stop, 'POST'), env);
  assert.equal(rowsOf(env)[0].status, 'paused');
});

await test('Slack channel assignment uses the stored hook', async () => {
  const { env } = clientEnv();
  await W.fetch(endpointsForm('ana@agency.co'), env);
  env.DB.sql.prepare('insert into slack_hooks (team, channel, url, created_at) values (?, ?, ?, ?)')
    .run('Agency Co', '#alerts', 'https://hooks.slack.com/services/T/B/x', Date.now());
  const link = await _test.signLink(env, { a: 'assign', c: 'agency-co--shop', d: 'sl:1', e: Date.now() + 9e6 });
  assert.match(await (await W.fetch(act(link), env)).text(), /Slack #alerts in Agency Co/);
  await W.fetch(act(link, 'POST'), env);
  const [e] = (await (await sentinelRead(env)).json()).endpoints;
  assert.equal(e.slack_webhook_url, 'https://hooks.slack.com/services/T/B/x');
  assert.equal(e.telegram_chat_id, '');
});

await test('group upgraded to a supergroup: endpoints follow the new chat id', async () => {
  const { env, sent } = clientEnv();
  await W.fetch(endpointsForm('ana@agency.co'), env);
  await W.fetch(tgUpdate(joined(-555, 'Small group')), env);
  await W.fetch(act(actLinks(sent.at(-1))[0], 'POST'), env);
  await W.fetch(tgUpdate({ message: { chat: { id: -555, title: 'Small group', type: 'group' }, migrate_to_chat_id: -100999 } }), env);
  assert.equal(rowsOf(env)[0].telegram_chat_id, '-100999');
  assert.match(sent.at(-1), /Updated 1 endpoint in the database automatically/);
});

await test('sentinel read API: token required; stored config cannot override core fields', async () => {
  const { env } = clientEnv();
  env.DB.sql.prepare(`insert into endpoints (client_id, endpoint_name, target_url, config, source, telegram_chat_id, status, created_at, updated_at)
    values ('a--b', 'API', 'https://a.co', ?, 'manual', '-1', 'active', 1, 1)`)
    .run(JSON.stringify({ client_id: 'x', target_url: 'https://evil.co', method: 'HEAD', json_rules: [{ path: 'ok', rule: 'exists' }] }));
  env.DB.sql.prepare(`insert into endpoints (client_id, endpoint_name, target_url, source, status, created_at, updated_at)
    values ('a--b', 'No chat yet', 'https://a.co/2', 'manual', 'active', 1, 1)`).run();
  assert.equal((await sentinelRead(env, 'wrong')).status, 401);
  assert.equal((await sentinelRead(env, null)).status, 401);
  const r = await sentinelRead(env);
  assert.equal(r.headers.get('cache-control'), 'no-store');
  const { endpoints } = await r.json();
  assert.equal(endpoints.length, 1);                      // no destination yet: not returned
  assert.equal(endpoints[0].client_id, 'a--b');
  assert.equal(endpoints[0].target_url, 'https://a.co');
  assert.equal(endpoints[0].method, 'HEAD');
  assert.deepEqual(endpoints[0].json_rules, [{ path: 'ok', rule: 'exists' }]);
  delete env.HEARTBEAT_READ_TOKEN;
  assert.equal((await sentinelRead(env, 'readtok')).status, 401);
});

console.log(`\nall ${n} checks passed`);
