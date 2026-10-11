// cronus-ticker: Cloudflare Worker that gives Cronus Sentinel a real 5-minute beat.
//
// GitHub's built-in schedule is best-effort (in practice every ~15-20 min), so
// this Worker's cron trigger (*/5 * * * *) asks GitHub to start the
// "Cronus Sentinel" workflow every 5 minutes via the workflow_dispatch API.
//
// Setup (Cloudflare dashboard → Workers & Pages → cronus-ticker):
//   Settings → Variables and Secrets → add secret GH_TOKEN
//     = a fine-grained GitHub token, repository access: only cronus-sentinel,
//       permission: Actions → Read and write. Nothing else.
//   Settings → Trigger events → Cron: */5 * * * *
//
// The public URL does not trigger anything; it only reports that the ticker exists.

const REPO = 'johnathangonzo88-droid/cronus-sentinel';
const WORKFLOW = 'sentinel.yml';

async function dispatch(env) {
  const res = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'cronus-ticker',
    },
    body: JSON.stringify({ ref: 'main', inputs: { dry_run: 'false' } }),
  });
  if (res.status !== 204) {
    throw new Error(`GitHub dispatch failed: ${res.status} ${(await res.text()).slice(0, 200)}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.GH_TOKEN) throw new Error('GH_TOKEN secret is not set');
    await dispatch(env);
  },
  async fetch() {
    return new Response('cronus-ticker: runs on a schedule only.\n', { headers: { 'content-type': 'text/plain' } });
  },
};
