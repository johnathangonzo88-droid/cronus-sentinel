# Cronus Sentinel

Headless endpoint and webhook-receiver monitoring for agencies and SaaS teams.
Runs on GitHub Actions every 5 minutes, confirms failures before alerting,
and routes each client's incidents to their own Telegram chat (Slack optional).

## Repository layout

```
sentinel.py                     # the monitoring engine (aiohttp + asyncio)
endpoints.json                  # EXAMPLE config — safe to commit
endpoints.schema.json           # JSON Schema for the config (editor autocomplete)
requirements.txt
.github/workflows/sentinel.yml  # 5-minute cron + manual trigger
tests/smoke_test.py             # offline test against a local fake server
```

## Deploy in 10 minutes

1. **Create the bot.** In Telegram, message `@BotFather` → `/newbot` → copy the token.
2. **Get each client's chat ID.** Add the bot to the client's Telegram group, send any
   message in the group, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`
   (groups start with `-100`).
3. **Push this repo to GitHub as a public repository** (free Actions minutes).
4. **Add two repository secrets** (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN` — the BotFather token
   - `ENDPOINTS_JSON` — your real config (same shape as `endpoints.json`)
5. **Test.** Actions → *Cronus Sentinel* → *Run workflow* with `dry_run` checked.
   Read the log. Then run once with `dry_run` unchecked to confirm a real
   Telegram delivery (point one endpoint at `https://httpbin.org/status/503`).
6. The cron takes over from there.

## Adding a client

Clients normally add themselves: the pilot and endpoint-list forms save their endpoints in
the cronus-forms database as pending, and you activate them with the one-click link in the
"Telegram connected" / "Slack connected" email. Sentinel reads the active ones every run
from `ENDPOINTS_URL` (`GET /api/sentinel/endpoints`, authorised with `HEARTBEAT_READ_TOKEN`)
and keeps the last good copy in its state file in case the Worker can't be reached.
ONBOARDING.md has the steps.

The `ENDPOINTS_JSON` secret still works and is monitored alongside the database list.
Required per endpoint: `client_id`, `endpoint_name`, `target_url`, and a
`telegram_chat_id` or `slack_webhook_url`. Optional: `method`, `timeout_ms`,
`failure_threshold`, `expected_status`, `enabled`, plus the content checks, heartbeat and
report fields below. `endpoints.schema.json` documents every field; for database clients
the same fields go in the endpoint's `config` JSON.

### Content checks: "200 OK, but broken"

A status code alone misses the worst failures: the server answers 200 but the app behind it
is broken. Any of these on an endpoint turns that into a 🟠 BROKEN RESPONSE incident:

| Field | Fails when | Example |
|---|---|---|
| `json_rules` | a JSON value is missing, empty, wrong, or out of range | `[{"path": "data.orders", "rule": "not_empty"}]` |
| `body_contains` | a phrase that should be there isn't | `["id=\"lead-form\""]` |
| `body_not_contains` | a phrase that shouldn't be there is | `["maintenance mode"]` |
| `error_keywords` | the page shows a common app error (fatal error, stack trace, "something went wrong"…) | `true` |
| `min_body_bytes` | the body is smaller than this | `500` |
| `size_drop_pct` | the body shrinks this % below its usual size (learned over ~1 hour) | `60` |

`json_rules` paths look like `data.items`, `$.data.items[0].id`; rules are `exists`,
`not_empty` (default), `equals`, `not_equals`, `min`, `max` (on a list, min/max compare its length).
Never point a check at something that creates real orders or leads: use a read or health
endpoint, a test mode, or a heartbeat.

### Heartbeats: Zapier, Make and cron jobs

No-code automations can't be pinged; they run on their own. So they check in with us instead:

1. `python sentinel.py new-heartbeat` prints a fresh id, the ping URL, and the line to register it.
2. Register it: Cloudflare → D1 → `cronus-onboarding` → Console → paste the `insert` line.
3. The client adds a final "Webhooks by Zapier" / Make HTTP step that calls the ping URL.
4. Add the printed entry to `ENDPOINTS_JSON`, with `expect_every_min` set to how often it runs.

If no check-in arrives within `expect_every_min` plus 10% grace (at least 5 minutes), the client
gets a 🔴 MISSED CHECK-IN card. If *our* status API can't be read, the endpoint is marked unknown,
never down, so a Cronus problem never pages a client. Needs the `HEARTBEAT_READ_TOKEN` secret on
both the `cronus-forms` Worker and this repo.

### Branded monthly report (white-label)

On the 1st of each month (14:00 UTC) each client gets one self-contained HTML page in their
Telegram group: uptime, checks run, issues detected, total downtime, a per-integration table and
an incident log. It carries the agency's name, colour and logo, not Cronus, so the agency can
forward it or print it to PDF for their client. Slack-only channels get the numbers as text.

Set on any endpoint of the client: `client_name`, `report_brand`, `report_color` (`#0e7490`),
`report_logo_url`, and optionally `revenue_per_hour`, which adds a clearly labelled estimate of
sales at risk during downtime. `"monthly_report": false` leaves an endpoint out. To send one now,
run the workflow with **monthly_report** ticked (counters are kept).

## How alerting works

| Event | Result |
|---|---|
| 1st failed probe | counter = 1, no alert |
| Re-probe 20 s later also fails (or next run fails) | counter = 2 → 🔴 INCIDENT |
| Still down | silent; reminder every 6 failed checks |
| First healthy probe | 🟢 RECOVERED with total downtime and cause, counter reset |

A probe fails on HTTP 5xx, HTTP 404, connection errors, or latency over
`timeout_ms` (default 3000). Counters persist between runs in the Actions cache.

### Certificate warnings and the weekly report

- **Certificates**: twice a day each HTTPS host's certificate is read. The client gets one
  🟡 warning 14 days and one 3 days before it expires (`CERT_WARN_DAYS`). Renewing resets them.
- **Weekly report**: every Monday at 14:00 UTC, each client's alert channel gets one 📊 report:
  uptime, checks, average latency, incidents and downtime per endpoint, days left on each
  certificate, and whether anything is down right now. Set `"weekly_summary": false` on an
  endpoint to leave it out. To send one now (for a demo or a pilot check-in), run the workflow
  with **weekly_report** ticked and `dry_run` unticked; this doesn't reset the week.

### What an incident card tells the client

Every field appears only when the check actually saw it, so nothing is guessed:

- **Header**: DOWN, SLOW or DEGRADED, the endpoint name, optional severity, then the exact method and URL.
- **When**: when it started and how long ago, plus the last healthy check, in UTC and the client's local time.
- **Evidence**: status with its name, latency vs. budget, the hosting/CDN seen in the headers
  (Cloudflare, Vercel, Netlify, Render, Fly.io, Heroku, Shopify), a request ID to search their logs for,
  and a short excerpt of the error body with keys, tokens and passwords redacted.
- **Where**: for Cloudflare 52x and Vercel platform errors, which side failed.
- **Next step**: advice specific to the failure (DNS, expired certificate, refused connection, each
  Cloudflare 52x code, 401/403, 405, 429, 502–504…).
- **Buttons**: Runbook and Dashboard, when those links are set.

## Public logs, private clients

The repo is public, so its Actions logs are public too. By default the log
shows endpoints as `#0`, `#1`… and never prints URLs, client names, chat IDs
or alert text. Each run ends with one `cronus-summary` annotation (counts and
indexes only) that Jarvis reads for its incident ledger and maps back to
names using the endpoint list it exported. Set `SENTINEL_VERBOSE_LOGS=1` only
when running locally.

## Run locally

```bash
pip install -r requirements.txt
python tests/smoke_test.py          # offline, no network needed
python tests/test_reports.py        # weekly report + certificate warnings
python tests/test_payload.py        # content checks, heartbeats, monthly report
(cd forms && node test.mjs)         # cronus-forms Worker, incl. heartbeat routes
DRY_RUN=1 SENTINEL_VERBOSE_LOGS=1 python sentinel.py   # real probes, alerts printed not sent
```
