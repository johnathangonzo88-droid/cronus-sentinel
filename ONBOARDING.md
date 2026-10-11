# Client onboarding checklist

Ten minutes per client. Do every step, in order. Nothing is "live" until step 5 passes.

## 1. Collect (one email or DM)

- [ ] Agency name and a short client id, lowercase with dashes (e.g. `northwind-agency`)
- [ ] Up to 3 URLs for a pilot, 25 for Pro Agency. For each: a human name ("Orders webhook receiver"), the URL, and the expected healthy status if it isn't a normal 2xx/3xx (e.g. a webhook receiver that answers `405` to GET)
- [ ] Where alerts go: a Telegram group (preferred) or a Slack incoming-webhook URL
- [ ] Pilot start date (today) → write down day 12 (check-in) and day 14 (pilot ends)

## 2. Get their Telegram chat id

1. They add the Cronus bot to their alert group and post any message in it.
2. You find the group id (starts with `-100`): forward that message to `@RawDataBot`,
   or open the bot's `getUpdates` page in your own browser. Never paste the bot token
   into chat tools, docs or this repo.

## 3. Update the endpoint list

The live list is the `ENDPOINTS_JSON` secret (GitHub → cronus-sentinel → Settings →
Secrets and variables → Actions). Updating it **replaces the whole list**, so:

1. Edit your private master copy (keep it outside this public repo).
2. Add one entry per URL:

   ```json
   {
     "client_id": "northwind-agency",
     "endpoint_name": "Orders webhook receiver",
     "target_url": "https://example.com/webhooks/orders",
     "telegram_chat_id": "-1001234567890",
     "expected_status": [200, 405]
   }
   ```

   Optional fields: `method`, `timeout_ms` (default 3000), `failure_threshold`
   (default 2), `slack_webhook_url`, `enabled`. See `endpoints.schema.json`.

   Worth asking every client for (they make alerts far more useful):
   - `timezone` — e.g. `"America/Chicago"`; alerts show their local time next to UTC.
   - `severity` — e.g. `"critical"` for checkout or payment paths; shown in the alert header.
   - `runbook_url` / `dashboard_url` — https links that become buttons on every alert
     (their runbook doc, hosting dashboard, or log search).
3. Check it's valid JSON (any JSON validator), then paste the full list into the secret.

## 4. Confirm the URLs are healthy now

Actions → Cronus Sentinel → latest run → the `cronus-summary` line. `n` should equal
the new endpoint count and `failing` should be `[]`. If a URL is failing on day one,
fix the config (usually `expected_status`) before the client sees an alert.

If `blocked` lists an endpoint, the client's bot protection (usually Cloudflare) answered
with a challenge page instead of their app, so Cronus can't see its health. Fixes, easiest first:
- Point the check at a URL that bot protection doesn't cover (an API or webhook path, or a
  health-check URL on a subdomain that isn't proxied).
- Cloudflare Pro or higher: a WAF custom rule that skips bot checks for the Cronus user agent.
- Cloudflare Free: Bot Fight Mode can't be bypassed per request; the client would have to turn
  it off (Security → Bots) or use one of the options above.

## 5. Send the test alert (the client watches it arrive)

Actions → Cronus Sentinel → **Run workflow** → untick `dry_run`, tick `test_alert` → Run.
Every alert channel gets one sample card marked TEST. The run's annotation says
`N/N test alert(s) delivered`. If anything failed, stop and fix it before go-live.

## 6. Tell the client, in writing

- What's watched (list the names), how often (every 5 minutes), and where alerts go.
- An alert means two failed checks 20 seconds apart. It is not an uptime guarantee.
- Pilot ends on day 14 (date). Link to the pilot terms: https://cronussignals.com/pilot-terms

## 7. Day 12 and day 14

- Day 12: run the workflow with **weekly_report** ticked (and `dry_run` unticked) so the client
  gets a fresh report in their channel, then send the payment link with a line pointing to it.
- Day 14: paid → keep running. Not paid → remove their entries from `ENDPOINTS_JSON` the
  same day and tell them it's off. Never keep monitoring a client who hasn't agreed to pay.

## Offboarding

Remove their entries from `ENDPOINTS_JSON`, confirm `n` dropped in the next run's summary,
and ask them to remove the bot from their group.
