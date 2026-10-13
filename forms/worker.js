// cronus-forms: the small backend behind cronussignals.com/api/*.
//
//   POST /api/pilot           the pilot form, emailed to the Cronus inbox.
//   POST /api/endpoints       a paying client's endpoint list (up to 25), emailed as a ready-to-paste list.
//   GET  /api/slack/install   "Add to Slack": sends the client to Slack to pick a channel.
//   GET  /api/slack/callback  Slack sends them back here; the channel's webhook URL is
//                             emailed to the Cronus inbox and a welcome line is posted.
//   POST /api/stripe/webhook  Stripe events: new paying client, failed payment, cancellation,
//                             each emailed to the Cronus inbox with what to do next.
//   GET|POST|HEAD /api/hb/<id>
//                             A client's Zap / Make scenario / cron job checks in. Only ids
//                             already registered in the heartbeats table are accepted.
//   GET  /api/hb/<id>/status  When that heartbeat last checked in (Bearer HEARTBEAT_READ_TOKEN);
//                             read by sentinel.py every 5 minutes.
//   POST /api/telegram/webhook Telegram: the alerts bot was added to / removed from a group.
//   GET|POST /api/admin/act   Signed one-click links from the inbox emails: activate a client's
//                             endpoints on a chat, pause or resume them.
//   GET  /api/sentinel/endpoints  Active endpoints from D1 (Bearer HEARTBEAT_READ_TOKEN); read
//                             by sentinel.py every run, alongside its ENDPOINTS_JSON secret.
//   daily cron                Emails a reminder about paid clients with no endpoint list after 48h.
//
// Setup (Cloudflare → Workers & Pages → cronus-forms):
//   Binding  MAILER  type "send_email", destination cronussignals@gmail.com (a verified
//                    Email Routing destination address)
//   Binding  DB      D1 database "cronus-onboarding" (schema.sql)
//   Route    cronussignals.com/api/*  → cronus-forms
//   Trigger  cron "7 14 * * *"  (daily, 9:07am Chicago in summer / 8:07am in winter)
//   Variable SLACK_CLIENT_ID        (plain text)  from the Cronus Slack app → Basic Information
//   Secret   SLACK_CLIENT_SECRET    (secret)      same page. Also signs the OAuth state.
//   Secret   STRIPE_WEBHOOK_SECRET  (secret)      whsec_... from Stripe → Developers → Webhooks
//   Secret   HEARTBEAT_READ_TOKEN   (secret)      any long random string; the same value goes in the
//                                                 cronus-sentinel GitHub secret of that name
//   Secret   ADMIN_LINK_SECRET      (secret)      any long random string; signs the one-click links
//
// Spam handling: a hidden honeypot field, a minimum fill time, length limits and URL checks.

import { EmailMessage } from 'cloudflare:email';

const TO = 'cronussignals@gmail.com';
const FROM = 'pilot@cronussignals.com';
const ORIGIN = 'https://cronussignals.com';
const SLACK_REDIRECT = `${ORIGIN}/api/slack/callback`;
const STATE_TTL_MS = 30 * 60 * 1000;

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });

// Header values must be single-line; strip anything that could inject headers.
const oneLine = (s, max) => String(s ?? '').replace(/[\r\n\t]+/g, ' ').trim().slice(0, max);
const isEmail = (s) => /^[^\s@<>"]+@[^\s@<>"]+\.[^\s@<>"]+$/.test(s);
const isUrl = (s) => { try { const u = new URL(s); return u.protocol === 'https:' || u.protocol === 'http:'; } catch { return false; } };
const encodeHeader = (s) => (/^[\x20-\x7e]*$/.test(s) ? s : `=?UTF-8?B?${btoa(String.fromCharCode(...new TextEncoder().encode(s)))}?=`);

function validate(d) {
  const f = {
    name: oneLine(d.name, 100),
    email: oneLine(d.email, 200),
    agency: oneLine(d.agency, 150),
    urls: [d.url1, d.url2, d.url3].map((u) => oneLine(u, 500)).filter(Boolean),
    channel: d.channel === 'slack' ? 'Slack' : 'Telegram',
    notes: String(d.notes ?? '').slice(0, 2000).trim(),
  };
  if (!f.name || !f.agency) return [null, 'Please add your name and agency.'];
  if (!isEmail(f.email)) return [null, 'Please use a valid email address.'];
  if (!f.urls.length) return [null, 'Please add at least one endpoint URL.'];
  if (f.urls.length > 3) return [null, 'A pilot covers up to 3 endpoints.'];
  if (!f.urls.every(isUrl)) return [null, 'Endpoint URLs must start with https:// or http://'];
  return [f, null];
}

function buildMessage(f, extra = []) {
  const id = `<${crypto.randomUUID()}@cronussignals.com>`;
  const body = [
    'New Cronus pilot request',
    '',
    `Name:     ${f.name}`,
    `Email:    ${f.email}`,
    `Agency:   ${f.agency}`,
    `Alerts:   ${f.channel}`,
    '',
    'Endpoints:',
    ...f.urls.map((u, i) => `  ${i + 1}. ${u}`),
    '',
    'Notes:',
    f.notes || '  (none)',
    '',
    ...(extra.length ? [...extra, ''] : []),
    '--',
    'Reply to this email to answer them directly. Next steps: ONBOARDING.md in cronus-sentinel.',
  ].join('\r\n');
  return [
    `From: Cronus pilot form <${FROM}>`,
    `To: ${TO}`,
    `Reply-To: ${f.email}`,
    `Subject: ${encodeHeader(`Pilot request: ${f.agency} (${f.urls.length} endpoint${f.urls.length > 1 ? 's' : ''})`)}`,
    `Message-ID: ${id}`,
    `Date: ${new Date().toUTCString()}`,
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: 8bit',
    '',
    body,
  ].join('\r\n');
}

// ---------------------------------------------------------------------------
// Pilot form
// ---------------------------------------------------------------------------

async function handlePilot(request, env) {
  if (request.method !== 'POST') return json({ ok: false, error: 'method not allowed' }, 405);
  const origin = request.headers.get('origin');
  if (origin && origin !== ORIGIN) return json({ ok: false, error: 'forbidden' }, 403);

  let d;
  try { d = await request.json(); } catch { return json({ ok: false, error: 'bad request' }, 400); }

  // Bots: filled the hidden field, or submitted faster than a person could type.
  if (d.website || Number(d.elapsed) < 3000) return json({ ok: true });

  const [f, err] = validate(d);
  if (err) return json({ ok: false, error: err }, 400);

  const clientId = `${slugId(f.agency) || 'agency'}--pilot`.slice(0, 64);
  const names = new Set();
  const rows = f.urls.map((u) => {
    let name = urlLabel(u);
    for (let k = 2; names.has(name.toLowerCase()); k++) name = `${urlLabel(u)} (${k})`;
    names.add(name.toLowerCase());
    return { client_id: clientId, endpoint_name: name, target_url: u, config: {} };
  });
  const saved = await quietly('save pilot', () => saveEndpoints(env, rows, { agency: f.agency, email: f.email, source: 'pilot' }), null);
  const extra = [...savedLines(saved, [clientId]), '', ...await quietly('pilot links', () => openDestinationLines(env, [clientId]), [])];

  try {
    await env.MAILER.send(new EmailMessage(FROM, TO, buildMessage(f, extra)));
  } catch (e) {
    console.log('send failed', String(e));
    return json({ ok: false, error: 'send failed' }, 502);
  }
  return json({ ok: true });
}

// ---------------------------------------------------------------------------
// Endpoint list (Pro Agency, up to 25)
// ---------------------------------------------------------------------------

const MAX_ENDPOINTS = 25;
const slugId = (s) => String(s ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);

function validateEndpoints(d) {
  const f = {
    name: oneLine(d.name, 100),
    email: oneLine(d.email, 200),
    agency: oneLine(d.agency, 150),
    notes: String(d.notes ?? '').slice(0, 2000).trim(),
    rows: [],
  };
  if (!f.name || !f.agency) return [null, 'Please add your name and agency.'];
  if (!isEmail(f.email)) return [null, 'Please use a valid email address.'];
  const raw = Array.isArray(d.rows) ? d.rows.slice(0, 60) : [];
  for (const [i, r] of raw.entries()) {
    const row = {
      client: oneLine(r?.client, 100),
      label: oneLine(r?.label, 100),
      url: oneLine(r?.url, 500),
      expect: oneLine(r?.expect, 40),
    };
    if (!row.client && !row.label && !row.url && !row.expect) continue; // blank line
    if (!row.url) return [null, `Row ${i + 1}: add the URL.`];
    if (!isUrl(row.url)) return [null, `Row ${i + 1}: the URL must start with https:// or http://`];
    if (!row.label) return [null, `Row ${i + 1}: give it a short name, like "Orders webhook".`];
    if (!row.client) return [null, `Row ${i + 1}: which of your clients is this for?`];
    const codes = row.expect ? row.expect.split(/[\s,]+/).filter(Boolean) : [];
    if (codes.some((c) => !/^[1-5]\d\d$/.test(c))) return [null, `Row ${i + 1}: healthy status codes look like 200 or 405, separated by commas.`];
    row.codes = codes.map(Number);
    if (f.rows.some((x) => x.client.toLowerCase() === row.client.toLowerCase() && x.label.toLowerCase() === row.label.toLowerCase())) {
      return [null, `Row ${i + 1}: "${row.label}" is already listed for ${row.client}. Give it a different name.`];
    }
    f.rows.push(row);
  }
  if (!f.rows.length) return [null, 'Please add at least one endpoint.'];
  if (f.rows.length > MAX_ENDPOINTS) return [null, `Pro Agency covers up to ${MAX_ENDPOINTS} endpoints.`];
  return [f, null];
}

// One D1 row per form row. client_id = agency-slug--client-slug.
function endpointRows(f) {
  const agencyId = slugId(f.agency) || 'agency';
  return f.rows.map((r) => ({
    client_id: `${agencyId}--${slugId(r.client) || 'client'}`.slice(0, 64),
    endpoint_name: r.label,
    target_url: r.url,
    config: r.codes.length ? { expected_status: r.codes } : {},
  }));
}

function savedLines(saved, clientIds) {
  if (!saved) return ['Could NOT save these to the database (see the Worker logs). Add them to ENDPOINTS_JSON by hand.'];
  const lines = [
    `Saved as pending for client_id ${clientIds.join(', ')}: ${plural(saved.saved, 'endpoint')}.`,
    'Nothing is checked until you activate it with one of the links below.',
  ];
  if (saved.unchanged.length) {
    lines.push('', 'Already active or paused, so NOT changed by this form (edit them in D1 if this is really them):',
      ...saved.unchanged.map((x) => `  - ${x}`));
  }
  return lines;
}

function endpointsMessage(f, extra = []) {
  const byClient = new Map();
  for (const r of f.rows) {
    if (!byClient.has(r.client)) byClient.set(r.client, []);
    byClient.get(r.client).push(r);
  }
  const body = [
    `Endpoint list from ${f.agency}`,
    '',
    `From:     ${f.name} <${f.email}>`,
    `Total:    ${f.rows.length} endpoint${f.rows.length === 1 ? '' : 's'} across ${byClient.size} client${byClient.size === 1 ? '' : 's'}`,
    '',
    ...[...byClient].flatMap(([client, rows]) => [
      `${client}`,
      ...rows.map((r) => `  - ${r.label}: ${r.url}${r.codes.length ? `  (healthy: ${r.codes.join(', ')})` : ''}`),
      '',
    ]),
    'Notes:',
    f.notes || '  (none)',
    '',
    ...extra,
    '',
    '--',
    'Reply to this email to write to them directly.',
  ].join('\r\n');
  return [
    `From: Cronus endpoints form <${FROM}>`,
    `To: ${TO}`,
    `Reply-To: ${f.email}`,
    `Subject: ${encodeHeader(`Endpoint list: ${f.agency} (${f.rows.length})`)}`,
    `Message-ID: <${crypto.randomUUID()}@cronussignals.com>`,
    `Date: ${new Date().toUTCString()}`,
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: 8bit',
    '',
    body,
  ].join('\r\n');
}

async function handleEndpoints(request, env) {
  if (request.method !== 'POST') return json({ ok: false, error: 'method not allowed' }, 405);
  const origin = request.headers.get('origin');
  if (origin && origin !== ORIGIN) return json({ ok: false, error: 'forbidden' }, 403);
  let d;
  try { d = await request.json(); } catch { return json({ ok: false, error: 'bad request' }, 400); }
  if (d.website || Number(d.elapsed) < 3000) return json({ ok: true });
  const [f, err] = validateEndpoints(d);
  if (err) return json({ ok: false, error: err }, 400);
  const rows = endpointRows(f);
  const ids = [...new Set(rows.map((r) => r.client_id))];
  const saved = await quietly('save endpoints', () => saveEndpoints(env, rows, { agency: f.agency, email: f.email, source: 'pro' }), null);
  const extra = [...savedLines(saved, ids), '', ...await quietly('endpoint links', () => openDestinationLines(env, ids), [])];
  try {
    await env.MAILER.send(new EmailMessage(FROM, TO, endpointsMessage(f, extra)));
  } catch (e) {
    console.log('send failed', String(e));
    return json({ ok: false, error: 'send failed' }, 502);
  }
  await markEndpointsReceived(env, f.email);
  return json({ ok: true, count: f.rows.length });
}

// ---------------------------------------------------------------------------
// Add to Slack (OAuth v2, incoming-webhook scope only)
// ---------------------------------------------------------------------------

const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return b64url(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(msg)));
}

async function makeState(secret, now = Date.now()) {
  const body = `${now}.${b64url(crypto.getRandomValues(new Uint8Array(12)))}`;
  return `${body}.${await hmac(secret, body)}`;
}

async function checkState(secret, state, now = Date.now()) {
  const parts = String(state ?? '').split('.');
  if (parts.length !== 3) return false;
  const [ts, nonce, sig] = parts;
  const age = now - Number(ts);
  if (!Number.isFinite(age) || age < 0 || age > STATE_TTL_MS) return false;
  const want = await hmac(secret, `${ts}.${nonce}`);
  if (want.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}

const back = (params) => Response.redirect(`${ORIGIN}/connect?${new URLSearchParams(params)}`, 302);

function slackEmail(team, hook, extra = []) {
  const body = [
    'A client connected Slack to Cronus.',
    '',
    `Workspace: ${team.name} (${team.id})`,
    `Channel:   ${hook.channel} (${hook.channel_id})`,
    '',
    'Webhook URL (treat it like a password):',
    hook.url,
    '',
    ...extra,
    '',
    '(Clients still in ENDPOINTS_JSON instead: add "slack_webhook_url" with the URL above.)',
    hook.configuration_url ? `They can remove it any time: ${hook.configuration_url}` : '',
  ].join('\r\n');
  return [
    `From: Cronus connect <${FROM}>`,
    `To: ${TO}`,
    `Subject: ${encodeHeader(oneLine(`Slack connected: ${team.name} ${hook.channel}`, 200))}`,
    `Message-ID: <${crypto.randomUUID()}@cronussignals.com>`,
    `Date: ${new Date().toUTCString()}`,
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: 8bit',
    '',
    body,
  ].join('\r\n');
}

const WELCOME =
  'Cronus Signals is connected to this channel. Once your endpoints are set up, ' +
  'confirmed incidents for them will post here, starting with a test alert so you know what one looks like.';

async function handleSlack(url, env) {
  const fetchImpl = (...a) => fetch(...a);
  if (!env.SLACK_CLIENT_ID || !env.SLACK_CLIENT_SECRET) return back({ slack: 'unavailable' });

  if (url.pathname === '/api/slack/install') {
    const q = new URLSearchParams({
      client_id: env.SLACK_CLIENT_ID,
      scope: 'incoming-webhook',
      redirect_uri: SLACK_REDIRECT,
      state: await makeState(env.SLACK_CLIENT_SECRET),
    });
    return Response.redirect(`https://slack.com/oauth/v2/authorize?${q}`, 302);
  }

  // /api/slack/callback
  if (url.searchParams.get('error')) return back({ slack: 'cancelled' });
  if (!(await checkState(env.SLACK_CLIENT_SECRET, url.searchParams.get('state')))) return back({ slack: 'expired' });
  const code = url.searchParams.get('code');
  if (!code) return back({ slack: 'error' });

  let data;
  try {
    const res = await fetchImpl('https://slack.com/api/oauth.v2.access', {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ client_id: env.SLACK_CLIENT_ID, client_secret: env.SLACK_CLIENT_SECRET, code, redirect_uri: SLACK_REDIRECT }),
    });
    data = await res.json();
  } catch (e) {
    console.log('slack token exchange failed', String(e));
    return back({ slack: 'error' });
  }
  const hook = data?.incoming_webhook;
  if (!data?.ok || !hook?.url?.startsWith('https://hooks.slack.com/')) {
    console.log('slack oauth not ok', data?.error ?? 'no incoming_webhook');
    return back({ slack: 'error' });
  }
  const team = { name: oneLine(data.team?.name ?? 'unknown workspace', 120), id: oneLine(data.team?.id ?? '', 40) };
  hook.channel = oneLine(hook.channel ?? '', 120);
  hook.channel_id = oneLine(hook.channel_id ?? '', 40);

  const hookId = await quietly('save slack hook', async () => {
    await env.DB.prepare(
      `insert into slack_hooks (team, channel, url, created_at) values (?, ?, ?, ?)
       on conflict (url) do update set team = excluded.team, channel = excluded.channel, created_at = excluded.created_at`,
    ).bind(team.name, hook.channel, hook.url, Date.now()).run();
    return (await env.DB.prepare('select id from slack_hooks where url = ?').bind(hook.url).first())?.id ?? null;
  }, null);
  const extra = hookId == null ? [] : await quietly('slack links', () => pendingClientLines(env, `sl:${hookId}`), []);

  // The webhook is only useful if it reaches the inbox: no email, no success page.
  try {
    await env.MAILER.send(new EmailMessage(FROM, TO, slackEmail(team, hook, extra)));
  } catch (e) {
    console.log('slack email failed', String(e));
    return back({ slack: 'error' });
  }
  try {
    await fetchImpl(hook.url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: WELCOME }) });
  } catch (e) {
    console.log('welcome post failed', String(e)); // not fatal: the URL is already in the inbox
  }
  return back({ slack: 'connected', channel: hook.channel, team: team.name });
}

// ---------------------------------------------------------------------------
// Stripe webhook
// ---------------------------------------------------------------------------

const STRIPE_TOLERANCE_S = 300;              // reject signatures older than 5 minutes (replays)
const STALE_CLAIM_MS = 5 * 60 * 1000;        // a 'processing' event older than this is assumed dead
const NUDGE_AFTER_MS = 48 * 60 * 60 * 1000;  // remind about missing endpoint lists after 48h

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');

function sameString(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Stripe signs `${timestamp}.${raw body bytes}`. Verify on the exact bytes received, never on re-serialized JSON.
async function verifyStripe(raw, header, secret, nowS = Math.floor(Date.now() / 1000)) {
  if (!header || !secret) return false;
  let t = null;
  const sigs = [];
  for (const part of header.split(',')) {
    const [k, v] = part.split('=', 2).map((s) => s?.trim());
    if (k === 't') t = Number(v);
    if (k === 'v1' && v) sigs.push(v);
  }
  if (!Number.isFinite(t) || !sigs.length || Math.abs(nowS - t) > STRIPE_TOLERANCE_S) return false;
  const prefix = new TextEncoder().encode(`${t}.`);
  const signed = new Uint8Array(prefix.length + raw.byteLength);
  signed.set(prefix, 0);
  signed.set(new Uint8Array(raw), prefix.length);
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const want = hex(await crypto.subtle.sign('HMAC', key, signed));
  return sigs.some((s) => sameString(s, want));
}

// Idempotency ledger. Returns 'mine' (process it), 'done' (already handled) or 'busy' (another attempt is running).
async function claimEvent(db, id, type, now = Date.now()) {
  const ins = await db.prepare('insert into stripe_events (event_id, type, received_at) values (?, ?, ?) on conflict (event_id) do nothing')
    .bind(id, type, now).run();
  if (ins.meta.changes === 1) return 'mine';
  const row = await db.prepare('select status, received_at, attempts from stripe_events where event_id = ?').bind(id).first();
  if (!row) return 'busy';                         // released between insert and select; let Stripe retry
  if (row.status === 'done') return 'done';
  if (now - row.received_at <= STALE_CLAIM_MS) return 'busy';
  const took = await db.prepare(
    "update stripe_events set received_at = ?, attempts = attempts + 1 where event_id = ? and status = 'processing' and attempts = ?",
  ).bind(now, id, row.attempts).run();
  return took.meta.changes === 1 ? 'mine' : 'busy';
}

const finishEvent = (db, id) =>
  db.prepare("update stripe_events set status = 'done', processed_at = ? where event_id = ?").bind(Date.now(), id).run();
const releaseEvent = (db, id) => db.prepare('delete from stripe_events where event_id = ?').bind(id).run();

function inboxEmail({ subject, replyTo, lines }) {
  return [
    `From: Cronus billing <${FROM}>`,
    `To: ${TO}`,
    ...(replyTo && isEmail(replyTo) ? [`Reply-To: ${oneLine(replyTo, 200)}`] : []),
    `Subject: ${encodeHeader(oneLine(subject, 200))}`,
    `Message-ID: <${crypto.randomUUID()}@cronussignals.com>`,
    `Date: ${new Date().toUTCString()}`,
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: 8bit',
    '',
    lines.join('\r\n'),
  ].join('\r\n');
}

const sendInbox = (env, msg) => env.MAILER.send(new EmailMessage(FROM, TO, inboxEmail(msg)));
const money = (cents, cur) => (Number.isFinite(cents) ? `${(cents / 100).toFixed(2)} ${String(cur ?? '').toUpperCase()}` : 'unknown');
const dash = (path) => `https://dashboard.stripe.com/${path}`;
const who = (c) => (c.name ? `${c.name} <${c.email}>` : c.email);

async function onCheckoutPaid(s, env) {
  if (s.payment_status !== 'paid' && s.payment_status !== 'no_payment_required') {
    console.log('checkout not paid yet', s.id, s.payment_status); // async_payment_succeeded will follow
    return;
  }
  const email = oneLine(s.customer_details?.email ?? s.customer_email, 200);
  if (!email) throw new Error(`session ${s.id} has no email`);
  await env.DB.prepare(
    `insert into clients (session_id, customer_id, subscription_id, email, name, plan, amount_total, currency, created_at)
     values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict (session_id) do nothing`,
  ).bind(
    s.id, s.customer ?? null, s.subscription ?? null, email,
    oneLine(s.customer_details?.name, 120) || null,
    oneLine(s.metadata?.plan ?? 'pro_agency', 40),
    Number.isFinite(s.amount_total) ? s.amount_total : null, s.currency ?? null, Date.now(),
  ).run();

  const c = await env.DB.prepare('select * from clients where session_id = ?').bind(s.id).first();
  if (c.notified_at) return;   // retry after the email already went out

  // Did they already send their endpoint list (e.g. form submitted before this event arrived)?
  const already = await env.DB.prepare('select 1 from clients where lower(email) = lower(?) and endpoints_received_at is not null limit 1')
    .bind(c.email).first();

  await sendInbox(env, {
    subject: `New Pro Agency client: ${c.name || c.email}`,
    replyTo: c.email,
    lines: [
      'Someone just paid for Cronus Pro Agency.',
      '',
      `Client:   ${who(c)}`,
      `Amount:   ${money(c.amount_total, c.currency)}`,
      c.customer_id ? `Customer: ${dash(`customers/${c.customer_id}`)}` : '',
      c.subscription_id ? `Sub:      ${dash(`subscriptions/${c.subscription_id}`)}` : '',
      '',
      'Stripe sent them to cronussignals.com/endpoints after paying.',
      already
        ? 'Their endpoint list has already arrived, in a separate "Endpoint list" email.'
        : 'Their endpoint list will arrive as a separate "Endpoint list" email. If it hasn\'t come in 48 hours you\'ll get a reminder.',
      'If they want Slack alerts, send them to cronussignals.com/connect.',
      '',
      'Reply to this email to write to them directly.',
    ].filter((l, i, a) => l !== '' || a[i - 1] !== ''),
  });
  await env.DB.prepare('update clients set notified_at = ?, endpoints_received_at = coalesce(endpoints_received_at, ?) where session_id = ?')
    .bind(Date.now(), already ? Date.now() : null, s.id).run();
}

async function onSubscriptionDeleted(sub, env) {
  const { results } = await env.DB.prepare('select * from clients where subscription_id = ?').bind(sub.id).all();
  if (!results.length) {
    await sendInbox(env, {
      subject: 'Subscription cancelled (no matching client)',
      lines: [
        'A Stripe subscription ended that this system has no record of.',
        '(It may have been created before automatic onboarding was switched on.)',
        '',
        `Subscription: ${dash(`subscriptions/${sub.id}`)}`,
        sub.customer ? `Customer:     ${dash(`customers/${sub.customer}`)}` : '',
        '',
        'Check who it is and remove their endpoints from ENDPOINTS_JSON.',
      ],
    });
    return;
  }
  for (const c of results) {
    await env.DB.prepare("update clients set status = 'cancelled', cancelled_at = coalesce(cancelled_at, ?) where session_id = ?")
      .bind(Date.now(), c.session_id).run();
    if (c.cancel_notified_at) continue;
    await sendInbox(env, {
      subject: `Cancelled: ${c.name || c.email} - remove their endpoints`,
      replyTo: c.email,
      lines: [
        'A Pro Agency subscription has ended and access should stop now.',
        '',
        `Client:   ${who(c)}`,
        `Started:  ${new Date(c.created_at).toUTCString()}`,
        sub.cancellation_details?.reason ? `Reason:   ${sub.cancellation_details.reason}` : '',
        sub.cancellation_details?.feedback ? `Feedback: ${sub.cancellation_details.feedback}` : '',
        '',
        'To do: stop monitoring their endpoints.',
        ...(await quietly('pause link', async () => {
          const link = await signLink(env, { a: 'pause_email', m: c.email, e: Date.now() + PAUSE_TTL_MS });
          return link ? ['  Pause everything they submitted (review first, one click):', `  ${link}`] : NO_LINKS;
        }, NO_LINKS)),
        `  Anything of theirs still in ENDPOINTS_JSON: remove it there (search "Endpoint list" from ${c.email}).`,
        '  Their Slack webhook goes unused; they can remove the Cronus app from their workspace any time.',
        '',
        'Reply to this email if you want to ask them why they left.',
      ],
    });
    await env.DB.prepare('update clients set cancel_notified_at = ? where session_id = ?').bind(Date.now(), c.session_id).run();
  }
}

async function onInvoicePaymentFailed(inv, env) {
  if (inv.billing_reason === 'subscription_create') return; // first payment failures never became clients
  await sendInbox(env, {
    subject: `Payment failed: ${inv.customer_name || inv.customer_email || 'a Pro Agency client'}`,
    replyTo: inv.customer_email,
    lines: [
      'A renewal payment failed. Stripe retries automatically and emails the client.',
      'No action needed unless it ends in a cancellation, which you\'ll get a separate email for.',
      '',
      `Client:   ${inv.customer_name ? `${inv.customer_name} <${inv.customer_email}>` : inv.customer_email ?? 'unknown'}`,
      `Amount:   ${money(inv.amount_due, inv.currency)}`,
      `Attempt:  ${inv.attempt_count ?? '?'}`,
      inv.hosted_invoice_url ? `Invoice:  ${inv.hosted_invoice_url}` : '',
    ],
  });
}

const STRIPE_HANDLERS = {
  'checkout.session.completed': onCheckoutPaid,
  'checkout.session.async_payment_succeeded': onCheckoutPaid,
  'customer.subscription.deleted': onSubscriptionDeleted,
  'invoice.payment_failed': onInvoicePaymentFailed,
};

async function handleStripe(request, env) {
  if (request.method !== 'POST') return json({ ok: false, error: 'method not allowed' }, 405);
  const raw = await request.arrayBuffer();          // raw bytes: never parse before verifying
  if (!(await verifyStripe(raw, request.headers.get('stripe-signature'), env.STRIPE_WEBHOOK_SECRET))) {
    return json({ ok: false, error: 'bad signature' }, 400);
  }
  let event;
  try { event = JSON.parse(new TextDecoder().decode(raw)); } catch { return json({ ok: false, error: 'bad payload' }, 400); }

  const handler = STRIPE_HANDLERS[event.type];
  if (!handler) return json({ received: true, ignored: event.type });
  if (!env.DB) {
    console.log('DB binding missing');
    return json({ ok: false, error: 'not configured' }, 500);  // Stripe retries for up to 3 days
  }

  const claim = await claimEvent(env.DB, event.id, event.type);
  if (claim === 'done') return json({ received: true, duplicate: true });
  if (claim === 'busy') return json({ received: true, in_progress: true }, 409);

  try {
    await handler(event.data.object, env);
  } catch (e) {
    console.log('stripe handler failed', event.id, event.type, String(e?.stack ?? e));
    await releaseEvent(env.DB, event.id);
    return json({ ok: false, error: 'processing failed' }, 500);
  }
  await finishEvent(env.DB, event.id);
  return json({ received: true });
}

async function markEndpointsReceived(env, email) {
  if (!env.DB) return;
  try {
    await env.DB.prepare('update clients set endpoints_received_at = ? where lower(email) = lower(?) and endpoints_received_at is null')
      .bind(Date.now(), email).run();
  } catch (e) {
    console.log('mark endpoints failed', String(e)); // never fail the form over bookkeeping
  }
}

// ---------------------------------------------------------------------------
// Daily check: paid clients who still haven't sent their endpoint list
// ---------------------------------------------------------------------------

async function dailyCheck(env, now = Date.now()) {
  if (!env.DB) return { nudged: 0 };
  const { results } = await env.DB.prepare(
    `select * from clients where status = 'active' and endpoints_received_at is null and nudged_at is null
     and created_at < ? order by created_at`,
  ).bind(now - NUDGE_AFTER_MS).all();
  if (!results.length) return { nudged: 0 };

  await sendInbox(env, {
    subject: `${results.length} paid client${results.length === 1 ? '' : 's'} with no endpoint list yet`,
    lines: [
      'These clients paid more than 48 hours ago but no endpoint list has come in from their email address.',
      '',
      ...results.map((c) => `  - ${who(c)}, paid ${new Date(c.created_at).toUTCString()}`),
      '',
      'Worth a short note pointing them to cronussignals.com/endpoints.',
      'If they used a different email on the form, their list may already be in your inbox.',
      'You get one reminder per client.',
    ],
  });
  const stamp = env.DB.prepare('update clients set nudged_at = ? where session_id = ?');
  await env.DB.batch(results.map((c) => stamp.bind(now, c.session_id)));
  return { nudged: results.length };
}

// ---------------------------------------------------------------------------
// Heartbeats: no-code automations (Zapier, Make, cron) check in; silence is the outage.
// Ids are registered by the Cronus operator (insert into heartbeats), so random ids cost
// nothing: an unknown id updates zero rows. Pings closer than HB_MIN_GAP_MS are counted
// once, which keeps a chatty Zap from using up the D1 free-tier write allowance.
// ---------------------------------------------------------------------------
const HB_ID = /^hb_[0-9a-f]{24}$/;
const HB_MIN_GAP_MS = 30 * 1000;

function safeEqual(a, b) {
  const x = new TextEncoder().encode(String(a));
  const y = new TextEncoder().encode(String(b));
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

async function handleHeartbeat(request, env, path, now = Date.now()) {
  const m = path.match(/^\/api\/hb\/([^/]+)(\/status)?$/);
  if (!m || !HB_ID.test(m[1])) return json({ ok: false, error: 'not found' }, 404);
  if (!env.DB) return json({ ok: false, error: 'not configured' }, 503);
  const id = m[1];

  if (m[2]) {  // status, for sentinel.py
    if (request.method !== 'GET') return json({ ok: false, error: 'method not allowed' }, 405);
    const auth = request.headers.get('authorization') || '';
    if (!env.HEARTBEAT_READ_TOKEN || !safeEqual(auth, `Bearer ${env.HEARTBEAT_READ_TOKEN}`)) {
      return json({ ok: false, error: 'unauthorized' }, 401);
    }
    const row = await env.DB.prepare('select last_ping, pings from heartbeats where id = ?').bind(id).first();
    if (!row) return json({ ok: false, error: 'not found' }, 404);
    return json({ ok: true, last_ping: row.last_ping, pings: row.pings });
  }

  if (!['GET', 'POST', 'HEAD', 'PUT'].includes(request.method)) {
    return json({ ok: false, error: 'method not allowed' }, 405);
  }
  const r = await env.DB.prepare(
    `update heartbeats set last_ping = ?, pings = pings + 1
     where id = ? and (last_ping is null or last_ping < ?)`,
  ).bind(now, id, now - HB_MIN_GAP_MS).run();
  if (!r.meta?.changes) {
    const known = await env.DB.prepare('select 1 from heartbeats where id = ?').bind(id).first();
    if (!known) return json({ ok: false, error: 'unknown heartbeat' }, 404);
  }
  return json({ ok: true });
}

// ---------------------------------------------------------------------------
// Clients: monitored endpoints live in D1 (table endpoints).
//
//   Forms save a client's endpoints as 'pending'. The "connected" emails (Telegram,
//   Slack) and the endpoint-list emails carry signed one-click links:
//     GET  /api/admin/act?t=...   shows what the link will do (safe for mail scanners)
//     POST /api/admin/act         does it: activate a client's endpoints on a chat,
//                                 pause them, or resume them
//   GET  /api/sentinel/endpoints  the active endpoints, read by sentinel.py every run
//                                 (Bearer HEARTBEAT_READ_TOKEN)
//
// Setup: Secret ADMIN_LINK_SECRET (any long random string) signs the links. Without
// it the emails say so and nothing can be activated by link.
// ---------------------------------------------------------------------------
const ASSIGN_TTL_MS = 14 * 24 * 60 * 60 * 1000;
const PAUSE_TTL_MS = 120 * 24 * 60 * 60 * 1000;
const RECENT_MS = 30 * 24 * 60 * 60 * 1000;
const CORE_FIELDS = ['client_id', 'endpoint_name', 'target_url', 'telegram_chat_id', 'slack_webhook_url', 'enabled'];

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// A short readable name for a URL, used when the pilot form gives no label.
function urlLabel(u) {
  try {
    const x = new URL(u);
    return oneLine(`${x.host}${x.pathname === '/' ? '' : x.pathname}`, 90);
  } catch {
    return oneLine(u, 90);
  }
}

// Bookkeeping must never stop an email or a form from going through.
async function quietly(what, fn, fallback) {
  try { return await fn(); } catch (e) { console.log(`${what} failed`, String(e?.stack ?? e)); return fallback; }
}

// Upsert rows as pending. A row that is already active or paused is left untouched: the
// forms are public, so a submission must never change what is being monitored.
async function saveEndpoints(env, rows, meta, now = Date.now()) {
  const out = { saved: 0, unchanged: [] };
  if (!env.DB) return null;
  const stmt = env.DB.prepare(
    `insert into endpoints (client_id, endpoint_name, target_url, config, agency, contact_email, source, status, created_at, updated_at)
     values (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
     on conflict (client_id, endpoint_name) do update set target_url = excluded.target_url, config = excluded.config,
       agency = excluded.agency, contact_email = excluded.contact_email, updated_at = excluded.updated_at
     where endpoints.status = 'pending'`,
  );
  for (const r of rows) {
    const res = await stmt.bind(r.client_id, r.endpoint_name, r.target_url, JSON.stringify(r.config ?? {}),
      meta.agency, meta.email, meta.source, now, now).run();
    if (res.meta.changes) out.saved += 1;
    else out.unchanged.push(`${r.client_id} / ${r.endpoint_name}`);
  }
  return out;
}

// ---- signed links ---------------------------------------------------------
const b64urlText = (s) => b64url(new TextEncoder().encode(s));
function unb64url(s) {
  const b = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4));
  return new TextDecoder().decode(Uint8Array.from(b, (c) => c.charCodeAt(0)));
}

async function signLink(env, payload) {
  if (!env.ADMIN_LINK_SECRET) return null;
  const body = b64urlText(JSON.stringify(payload));
  return `${ORIGIN}/api/admin/act?t=${body}.${await hmac(env.ADMIN_LINK_SECRET, body)}`;
}

async function readLink(env, token, now = Date.now()) {
  if (!env.ADMIN_LINK_SECRET) return null;
  const parts = String(token ?? '').split('.');
  if (parts.length !== 2 || !parts[0] || !parts[1]) return null;
  if (!sameString(await hmac(env.ADMIN_LINK_SECRET, parts[0]), parts[1])) return null;
  let p;
  try { p = JSON.parse(unb64url(parts[0])); } catch { return null; }
  if (!p || typeof p !== 'object' || !(Number(p.e) > now)) return null;
  return p;
}

const NO_LINKS = ['(One-click links are off: set the ADMIN_LINK_SECRET secret on the cronus-forms Worker.)'];

// ---- destinations (Telegram groups, Slack channels) -----------------------
async function destInfo(env, d) {
  const [kind, id] = String(d ?? '').split(/:(.*)/s);
  if (kind === 'tg') {
    const row = await env.DB.prepare('select chat_id, title, status from telegram_chats where chat_id = ?').bind(id).first();
    if (!row || row.status !== 'active') return null;
    return { label: `Telegram group "${row.title || row.chat_id}"`, tg: row.chat_id };
  }
  if (kind === 'sl') {
    const row = await env.DB.prepare('select id, team, channel, url from slack_hooks where id = ?').bind(Number(id)).first();
    if (!row) return null;
    return { label: `Slack ${row.channel || 'channel'} in ${row.team || 'their workspace'}`, sl: row.url };
  }
  return null;
}

// Lines for a "connected" email: one activate link per client still waiting for a channel.
async function pendingClientLines(env, dest, now = Date.now()) {
  if (!env.DB) return [];
  const { results } = await env.DB.prepare(
    `select client_id, count(*) as n, max(agency) as agency, max(contact_email) as email, max(created_at) as at
     from endpoints where status = 'pending' and created_at > ? group by client_id order by at desc limit 10`,
  ).bind(now - RECENT_MS).all();
  if (!results.length) {
    return ['No endpoint lists are waiting right now. When one arrives, its email will have a link to this chat.'];
  }
  if (!env.ADMIN_LINK_SECRET) return NO_LINKS;
  const lines = ['Whose alerts should go here? Click to review, then activate:', ''];
  for (const r of results) {
    lines.push(`  ${r.client_id}  (${plural(r.n, 'endpoint')}, ${r.agency || '?'}, ${r.email || '?'})`);
    lines.push(`  ${await signLink(env, { a: 'assign', c: r.client_id, d: dest, e: now + ASSIGN_TTL_MS })}`, '');
  }
  return lines;
}

// Lines for an endpoint-list email: links to send each client's alerts to a chat that
// was connected recently and isn't used by anyone yet.
async function openDestinationLines(env, clientIds, now = Date.now()) {
  if (!env.DB) return [];
  const since = now - RECENT_MS;
  const tg = (await env.DB.prepare(
    `select chat_id, title from telegram_chats where status = 'active' and updated_at > ?
     and chat_id not in (select telegram_chat_id from endpoints where telegram_chat_id is not null)
     order by updated_at desc limit 5`,
  ).bind(since).all()).results;
  const sl = (await env.DB.prepare(
    `select id, team, channel from slack_hooks where created_at > ?
     and url not in (select slack_webhook_url from endpoints where slack_webhook_url is not null)
     order by created_at desc limit 5`,
  ).bind(since).all()).results;
  const dests = [
    ...tg.map((t) => ({ d: `tg:${t.chat_id}`, label: `Telegram group "${t.title || t.chat_id}"` })),
    ...sl.map((s) => ({ d: `sl:${s.id}`, label: `Slack ${s.channel || 'channel'} in ${s.team || 'their workspace'}` })),
  ];
  if (!dests.length) {
    return ['When they add @cronus_alerts_bot or connect Slack, that email will have the link to activate them.'];
  }
  if (!env.ADMIN_LINK_SECRET) return NO_LINKS;
  const lines = ['Already connected and not yet in use. If one of these is theirs, click to review and activate:', ''];
  for (const c of clientIds) {
    for (const x of dests) {
      lines.push(`  ${c} -> ${x.label}`);
      lines.push(`  ${await signLink(env, { a: 'assign', c, d: x.d, e: now + ASSIGN_TTL_MS })}`, '');
    }
  }
  return lines;
}

// ---- the action page -------------------------------------------------------
function page(title, bodyHtml, status = 200) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>${esc(title)}</title><style>
body{font:16px/1.5 system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#1b1f24;background:#fff}
h1{font-size:22px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #ddd;padding:6px 4px;text-align:left;vertical-align:top;word-break:break-all}
.note{background:#fff8e1;border:1px solid #f0d58a;padding:10px 12px;border-radius:6px}button{font:inherit;padding:10px 18px;border-radius:6px;border:0;background:#1f6feb;color:#fff;cursor:pointer}
button.stop{background:#c62828}code{background:#f3f4f6;padding:1px 5px;border-radius:4px}
@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}td,th{border-color:#30363d}.note{background:#2b2410;border-color:#6b5a1e}code{background:#21262d}}
</style></head><body>${bodyHtml}</body></html>`;
  return new Response(html, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store', 'x-robots-tag': 'noindex',
      'referrer-policy': 'no-referrer', 'x-frame-options': 'DENY',
      'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    },
  });
}

const ACTIONS = {
  assign: { verb: 'Activate monitoring', where: 'client_id = ?', arg: (p) => p.c },
  pause: { verb: 'Pause monitoring', where: "client_id = ? and status = 'active'", arg: (p) => p.c },
  resume: { verb: 'Resume monitoring', where: "client_id = ? and status = 'paused'", arg: (p) => p.c },
  pause_email: { verb: 'Pause monitoring', where: "lower(contact_email) = lower(?) and status = 'active'", arg: (p) => p.m },
};

function endpointTable(rows) {
  return `<table><tr><th>Client</th><th>Endpoint</th><th>URL</th><th>Now</th></tr>${rows.map((r) =>
    `<tr><td>${esc(r.client_id)}</td><td>${esc(r.endpoint_name)}</td><td>${esc(r.target_url)}</td><td>${esc(r.status)}</td></tr>`).join('')}</table>`;
}

async function handleAdminAct(request, env, now = Date.now()) {
  if (!['GET', 'POST'].includes(request.method)) return json({ ok: false, error: 'method not allowed' }, 405);
  if (!env.DB) return page('Not configured', '<h1>Not configured</h1><p>The database binding is missing.</p>', 503);
  let token = new URL(request.url).searchParams.get('t');
  if (request.method === 'POST') {
    try { token = (await request.formData()).get('t'); } catch { token = null; }
  }
  const p = await readLink(env, token, now);
  const act = p && ACTIONS[p.a];
  if (!act) {
    return page('Link expired', '<h1>This link has expired or isn\'t valid</h1><p>Activate links last 14 days. ' +
      'The newest "connected" or "Endpoint list" email has a fresh one.</p>', 400);
  }
  const { results: rows } = await env.DB.prepare(
    `select client_id, endpoint_name, target_url, status, agency, contact_email, source from endpoints where ${act.where}
     order by client_id, endpoint_name`,
  ).bind(act.arg(p)).all();
  const dest = p.a === 'assign' ? await destInfo(env, p.d) : null;
  if (p.a === 'assign' && !dest) {
    return page('Chat unavailable', '<h1>That chat is no longer available</h1><p>The bot was removed from the group, ' +
      'or the group changed its ID. The newest "Telegram connected" email has the current one.</p>', 409);
  }
  if (!rows.length) {
    return page('Nothing to do', `<h1>Nothing to do</h1><p>No endpoints match this link any more${p.c ? ` for <code>${esc(p.c)}</code>` : ''}. ` +
      'They may already be paused, or were removed.</p>');
  }

  const who = rows[0].contact_email ? ` from ${esc(rows[0].agency || '')} &lt;${esc(rows[0].contact_email)}&gt;` : '';
  if (request.method === 'GET') {
    const intro = {
      assign: `<p>Send confirmed incidents for <code>${esc(p.c)}</code>${who} to <b>${esc(dest?.label)}</b>.</p>
<div class="note">Check every URL below is safe to call every 5 minutes. A checkout, payment or form URL that
creates a real order or lead when called must not be monitored. Ask for a health or read-only endpoint instead.</div>`,
      pause: `<p>Stop checking these endpoints and stop their alerts. Nothing is deleted; you can resume later.</p>`,
      resume: `<p>Start checking these endpoints again.</p>`,
      pause_email: `<p>Stop checking every endpoint submitted from <b>${esc(p.m)}</b>. Nothing is deleted.</p>`,
    }[p.a];
    return page(act.verb, `<h1>${esc(act.verb)}?</h1>${intro}<p>${plural(rows.length, 'endpoint')}:</p>${endpointTable(rows)}
<form method="post" action="/api/admin/act"><input type="hidden" name="t" value="${esc(token)}">
<p><button${p.a.startsWith('pause') ? ' class="stop"' : ''}>${esc(act.verb)}</button></p></form>`);
  }

  // POST: do it.
  let changed;
  if (p.a === 'assign') {
    const set = dest.tg ? 'telegram_chat_id = ?' : 'slack_webhook_url = ?';
    changed = (await env.DB.prepare(
      `update endpoints set ${set}, status = 'active', activated_at = coalesce(activated_at, ?), updated_at = ? where ${act.where}`,
    ).bind(dest.tg ?? dest.sl, now, now, act.arg(p)).run()).meta.changes;
  } else {
    const to = p.a === 'resume' ? 'active' : 'paused';
    changed = (await env.DB.prepare(`update endpoints set status = ?, updated_at = ? where ${act.where}`)
      .bind(to, now, act.arg(p)).run()).meta.changes;
  }
  console.log('admin action', p.a, changed);

  const clients = [...new Set(rows.map((r) => r.client_id))];
  const later = [];
  for (const c of clients) {
    const flip = p.a === 'pause' || p.a === 'pause_email'
      ? await signLink(env, { a: 'resume', c, e: now + PAUSE_TTL_MS })
      : await signLink(env, { a: 'pause', c, e: now + PAUSE_TTL_MS });
    later.push(`<li><code>${esc(c)}</code>: <a href="${esc(flip)}">${p.a.startsWith('pause') ? 'resume' : 'pause'} later</a></li>`);
  }
  const next = p.a === 'assign' || p.a === 'resume'
    ? `<p>Sentinel picks ${changed === 1 ? 'it' : 'them'} up on its next run, within about 5 minutes.</p>
<p><b>Next:</b> GitHub → Actions → Cronus Sentinel → Run workflow, tick <code>test_alert</code>, and type
<code>${esc(clients[0])}</code> in <code>test_client</code>, so they see a TEST card.</p>`
    : '<p>Sentinel stops checking them on its next run. Their history is kept.</p>';
  return page('Done', `<h1>Done: ${plural(changed, 'endpoint')} ${p.a === 'assign' || p.a === 'resume' ? 'active' : 'paused'}</h1>
${next}<p>Bookmark these if useful:</p><ul>${later.join('')}</ul>`);
}

// ---- read API for sentinel.py ---------------------------------------------
async function handleSentinelEndpoints(request, env) {
  if (request.method !== 'GET') return json({ ok: false, error: 'method not allowed' }, 405);
  const auth = request.headers.get('authorization') || '';
  if (!env.HEARTBEAT_READ_TOKEN || !safeEqual(auth, `Bearer ${env.HEARTBEAT_READ_TOKEN}`)) {
    return json({ ok: false, error: 'unauthorized' }, 401);
  }
  if (!env.DB) return json({ ok: false, error: 'not configured' }, 503);
  const { results } = await env.DB.prepare(
    `select * from endpoints where status = 'active' and (telegram_chat_id is not null or slack_webhook_url is not null)
     order by client_id, endpoint_name`,
  ).all();
  const endpoints = results.map((r) => {
    let extra = {};
    try { extra = JSON.parse(r.config || '{}'); } catch { extra = {}; }
    if (!extra || typeof extra !== 'object' || Array.isArray(extra)) extra = {};
    for (const k of CORE_FIELDS) delete extra[k];
    return {
      ...extra,
      client_id: r.client_id,
      endpoint_name: r.endpoint_name,
      target_url: r.target_url,
      telegram_chat_id: r.telegram_chat_id || '',
      ...(r.slack_webhook_url ? { slack_webhook_url: r.slack_webhook_url } : {}),
    };
  });
  return json({ version: 1, endpoints });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/api/pilot') return handlePilot(request, env);
    if (url.pathname === '/api/endpoints') return handleEndpoints(request, env);
    if (url.pathname === '/api/stripe/webhook') return handleStripe(request, env);
    if (url.pathname === '/api/telegram/webhook') return handleTelegram(request, env);
    if (url.pathname.startsWith('/api/hb/')) return handleHeartbeat(request, env, url.pathname);
    if (url.pathname === '/api/admin/act') return handleAdminAct(request, env);
    if (url.pathname === '/api/sentinel/endpoints') return handleSentinelEndpoints(request, env);
    if (url.pathname === '/api/slack/install' || url.pathname === '/api/slack/callback') {
      if (request.method !== 'GET') return json({ ok: false, error: 'method not allowed' }, 405);
      return handleSlack(url, env);
    }
    return json({ ok: false, error: 'not found' }, 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(dailyCheck(env).then((r) => console.log('daily check', JSON.stringify(r))));
  },
};

// ---------------------------------------------------------------------------
// Telegram: when a client adds @cronus_alerts_bot to their group, the group's chat ID
// is saved to D1 (telegram_chats) and emailed to the Cronus inbox, ready to paste.
//
//   POST /api/telegram/webhook   Telegram calls this (set up once with setWebhook).
//
// Setup (Cloudflare → Workers & Pages → cronus-forms → Settings → Variables and Secrets):
//   Secret TELEGRAM_ALERTS_BOT_TOKEN  the Cronus Alerts token (same value as the GitHub secret)
//   Secret TELEGRAM_WEBHOOK_SECRET    any random string of A-Z a-z 0-9 _ - (up to 256 chars);
//                                     the same value goes in setWebhook's secret_token
// ---------------------------------------------------------------------------

const TG_JOINED = new Set(['member', 'administrator']);
const TG_GONE = new Set(['left', 'kicked']);

const TG_WELCOME =
  'Cronus Signals is connected to this group. Once your endpoints are set up, ' +
  'confirmed incidents will post here, starting with a test alert so you know what one looks like.';

function tgWho(u) {
  if (!u) return null;
  const name = oneLine([u.first_name, u.last_name].filter(Boolean).join(' '), 120);
  return u.username ? `${name} (@${oneLine(u.username, 64)})` : name || null;
}

function tgEmail(subject, lines) {
  return [
    `From: Cronus connect <${FROM}>`,
    `To: ${TO}`,
    `Subject: ${encodeHeader(oneLine(subject, 200))}`,
    `Message-ID: <${crypto.randomUUID()}@cronussignals.com>`,
    `Date: ${new Date().toUTCString()}`,
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: 8bit',
    '',
    lines.join('\r\n'),
  ].join('\r\n');
}

const tgMail = (env, subject, lines) => env.MAILER.send(new EmailMessage(FROM, TO, tgEmail(subject, lines)));

async function tgSend(env, chatId, text) {
  if (!env.TELEGRAM_ALERTS_BOT_TOKEN) return;
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_ALERTS_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text }),
    });
  } catch (e) {
    console.log('telegram welcome failed', String(e)); // not fatal: the ID is already saved and emailed
  }
}

async function tgRecord(env, chat, status, by, now) {
  if (!env.DB) return;
  await env.DB.prepare(
    `insert into telegram_chats (chat_id, title, type, status, added_by, first_seen, updated_at)
     values (?, ?, ?, ?, ?, ?, ?)
     on conflict (chat_id) do update set title = excluded.title, type = excluded.type,
       status = excluded.status, added_by = coalesce(excluded.added_by, telegram_chats.added_by),
       updated_at = excluded.updated_at`,
  ).bind(String(chat.id), oneLine(chat.title, 200), oneLine(chat.type, 20), status, by, now, now).run();
}

async function handleTelegram(request, env, now = Date.now()) {
  if (request.method !== 'POST') return json({ ok: false, error: 'method not allowed' }, 405);
  const got = request.headers.get('x-telegram-bot-api-secret-token') || '';
  if (!env.TELEGRAM_WEBHOOK_SECRET || !safeEqual(got, env.TELEGRAM_WEBHOOK_SECRET)) {
    return json({ ok: false, error: 'unauthorized' }, 401);
  }
  let u;
  try { u = await request.json(); } catch { return json({ ok: true }); }

  try {
    // The bot was added to, or removed from, a group.
    const cm = u.my_chat_member;
    if (cm && (cm.chat?.type === 'group' || cm.chat?.type === 'supergroup')) {
      const was = cm.old_chat_member?.status;
      const is = cm.new_chat_member?.status;
      const by = tgWho(cm.from);
      const title = oneLine(cm.chat.title, 200);
      const id = String(cm.chat.id);

      if (TG_JOINED.has(is) && !TG_JOINED.has(was)) {
        await tgRecord(env, cm.chat, 'active', by, now);
        const extra = await quietly('telegram links', () => pendingClientLines(env, `tg:${id}`, now), []);
        await tgMail(env, `Telegram connected: ${title}`, [
          'A Telegram group just added @cronus_alerts_bot.',
          '',
          `Group:     ${title}`,
          `Chat ID:   ${id}`,
          `Added by:  ${by ?? 'unknown'}`,
          '',
          ...extra,
          '',
          `(Clients still in ENDPOINTS_JSON instead: "telegram_chat_id": "${id}")`,
        ]);
        await tgSend(env, id, TG_WELCOME);
      } else if (TG_GONE.has(is) && !TG_GONE.has(was)) {
        await tgRecord(env, cm.chat, 'removed', null, now);
        const using = await quietly('count chat use', async () => (await env.DB.prepare(
          "select count(*) as n from endpoints where telegram_chat_id = ? and status = 'active'").bind(id).first())?.n ?? 0, 0);
        await tgMail(env, `Telegram removed: ${title}`, [
          '@cronus_alerts_bot was removed from a client group.',
          '',
          `Group:     ${title}`,
          `Chat ID:   ${id}`,
          `Removed by: ${by ?? 'unknown'}`,
          '',
          `Active endpoints alerting this chat: ${using}. Alerts to it will fail until they add the bot back.`,
          'If they have left Cronus, pause them (the cancellation email has the link) or remove them from ENDPOINTS_JSON.',
        ]);
      }
    }

    // A group that upgrades to a supergroup gets a NEW chat ID, and the old one stops working.
    const m = u.message;
    if (m?.migrate_to_chat_id) {
      const oldId = String(m.chat.id);
      const newId = String(m.migrate_to_chat_id);
      const title = oneLine(m.chat.title, 200);
      if (env.DB) {
        await env.DB.prepare("update telegram_chats set status = 'migrated', updated_at = ? where chat_id = ?")
          .bind(now, oldId).run();
      }
      await tgRecord(env, { id: newId, title, type: 'supergroup' }, 'active', null, now);
      const moved = !env.DB ? 0 : (await env.DB.prepare('update endpoints set telegram_chat_id = ?, updated_at = ? where telegram_chat_id = ?')
        .bind(newId, now, oldId).run()).meta.changes;
      await tgMail(env, `Telegram chat ID changed: ${title}`, [
        'A client group was upgraded by Telegram and its chat ID changed.',
        `Updated ${plural(moved, 'endpoint')} in the database automatically.`,
        'Any of theirs still in ENDPOINTS_JSON: update the ID there now, or alerts will fail.',
        '',
        `Group:   ${title}`,
        `Old ID:  ${oldId}`,
        `New ID:  ${newId}`,
        '',
        'Replace in their endpoints:',
        `  "telegram_chat_id": "${newId}"`,
      ]);
    }
  } catch (e) {
    console.log('telegram update failed', String(e?.stack ?? e));
    return json({ ok: false }, 500); // Telegram retries
  }
  return json({ ok: true });
}
