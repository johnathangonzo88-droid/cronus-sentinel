// cronus-forms: receives the pilot form on cronussignals.com/api/pilot and emails it
// to the Cronus inbox through Cloudflare Email Routing. No database, no third parties.
//
// Setup (Cloudflare → Workers & Pages → cronus-forms):
//   Binding  MAILER  type "send_email", destination cronussignals@gmail.com (a verified
//                    Email Routing destination address)
//   Route    cronussignals.com/api/*  → cronus-forms
//
// Spam handling: a hidden honeypot field, a minimum fill time, length limits and URL checks.

import { EmailMessage } from 'cloudflare:email';

const TO = 'cronussignals@gmail.com';
const FROM = 'pilot@cronussignals.com';
const ORIGIN = 'https://cronussignals.com';

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

function buildMessage(f) {
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/api/pilot') return json({ ok: false, error: 'not found' }, 404);
    if (request.method !== 'POST') return json({ ok: false, error: 'method not allowed' }, 405);
    const origin = request.headers.get('origin');
    if (origin && origin !== ORIGIN) return json({ ok: false, error: 'forbidden' }, 403);

    let d;
    try { d = await request.json(); } catch { return json({ ok: false, error: 'bad request' }, 400); }

    // Bots: filled the hidden field, or submitted faster than a person could type.
    if (d.website || Number(d.elapsed) < 3000) return json({ ok: true });

    const [f, err] = validate(d);
    if (err) return json({ ok: false, error: err }, 400);

    try {
      await env.MAILER.send(new EmailMessage(FROM, TO, buildMessage(f)));
    } catch (e) {
      console.log('send failed', String(e));
      return json({ ok: false, error: 'send failed' }, 502);
    }
    return json({ ok: true });
  },
};
