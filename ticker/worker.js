// cronus-ticker: Cloudflare Worker that gives Cronus Sentinel a real 5-minute beat
// and watches that the monitor itself is still running.
//
// Every 5 minutes (cron trigger */5 * * * *) it:
//   1. Asks GitHub to start the "Cronus Sentinel" workflow (workflow_dispatch API).
//      GitHub's own schedule is best-effort (in practice every ~15-20 min).
//   2. Watchdog: looks up the last successful monitor run. If none has finished
//      in 15 minutes, or the dispatch itself fails, it sends a Telegram message to
//      the owner. It re-alerts once an hour while the problem lasts.
//
// Setup (Cloudflare dashboard → Workers & Pages → cronus-ticker → Settings):
//   Secret   GH_TOKEN     fine-grained GitHub token, only cronus-sentinel,
//                         permission Actions → Read and write. Nothing else.
//   Secret   TG_BOT_TOKEN the Cronus Telegram bot token (same one GitHub uses).
//                         Without it the watchdog stays silent; the ticker still runs.
//   Variable TG_CHAT_ID   the owner's Telegram chat id.
//   Trigger  Cron */5 * * * *
//
// The public URL does not trigger anything; it only reports that the ticker exists.

const REPO = 'johnathangonzo88-droid/cronus-sentinel';
const WORKFLOW = 'sentinel.yml';
const STALE_MIN = 15;

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'cronus-ticker',
      ...(init.headers || {}),
    },
  });
}

async function dispatch(env) {
  const res = await gh(env, `/actions/workflows/${WORKFLOW}/dispatches`, {
    method: 'POST',
    body: JSON.stringify({ ref: 'main', inputs: { dry_run: 'false' } }),
  });
  if (res.status !== 204) {
    throw new Error(`GitHub dispatch failed: ${res.status} ${(await res.text()).slice(0, 200)}`);
  }
}

// Minutes since the last monitor run that finished successfully (null = unknown).
async function minutesSinceLastSuccess(env) {
  const res = await gh(env, `/actions/workflows/${WORKFLOW}/runs?per_page=20&status=success`);
  if (!res.ok) return null;
  const runs = (await res.json()).workflow_runs || [];
  if (!runs.length) return null;
  const latest = Math.max(...runs.map((r) => Date.parse(r.updated_at)));
  return (Date.now() - latest) / 60000;
}

async function telegram(env, text) {
  if (!env.TG_BOT_TOKEN || !env.TG_CHAT_ID) {
    console.log('watchdog: TG_BOT_TOKEN / TG_CHAT_ID not set, alert not sent:', text);
    return;
  }
  await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: env.TG_CHAT_ID, text, parse_mode: 'HTML' }),
  });
}

// Alert once when a problem starts, then once an hour. Each tick is 5 minutes, so
// "the first tick past a boundary" is a window of 5 minutes.
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

const firstTickPast = (minutes, boundary) => minutes >= boundary && minutes < boundary + 5;

async function tick(env) {
  let dispatchError = null;
  try {
    await dispatch(env);
  } catch (err) {
    dispatchError = err;
  }

  const age = await minutesSinceLastSuccess(env);
  const stale = age !== null && age >= STALE_MIN;
  const hourly = age !== null && (firstTickPast(age, STALE_MIN) || (age > 60 && age % 60 < 5));

  if (stale && hourly) {
    await telegram(env,
      '⚠️ <b>CRONUS WATCHDOG</b>\n' +
      `No successful monitor run in ${Math.round(age)} minutes. Client checks may not be running.\n` +
      (dispatchError ? `Ticker error: <code>${esc(String(dispatchError.message).slice(0, 150))}</code>\n` : '') +
      'Check GitHub → cronus-sentinel → Actions.');
  } else if (dispatchError && new Date().getUTCMinutes() < 5) {
    // Dispatch failing but recent runs still fine (backup schedule): tell the owner hourly.
    await telegram(env,
      '⚠️ <b>CRONUS WATCHDOG</b>\nThe 5-minute ticker could not start the monitor ' +
      `(<code>${esc(String(dispatchError.message).slice(0, 150))}</code>). ` +
      'Checks are falling back to GitHub’s slower schedule. The GitHub token may have expired.');
  }

  if (dispatchError) throw dispatchError; // still surface it in Cloudflare's logs
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.GH_TOKEN) throw new Error('GH_TOKEN secret is not set');
    await tick(env);
  },
  async fetch() {
    return new Response('cronus-ticker: runs on a schedule only.\n', { headers: { 'content-type': 'text/plain' } });
  },
};
