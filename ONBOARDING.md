# Client onboarding checklist

A few minutes per client. Do every step, in order. Nothing is "live" until step 5 passes.

## 1. They send their endpoints (no copying on your side)

- **Pilot:** the pilot form on cronussignals.com (up to 3 URLs). Saved as client
  `agency-name--pilot`.
- **Pro Agency:** cronussignals.com/endpoints after paying (up to 25). Saved as
  `agency-name--client-name` per client site.

Either way the endpoints are stored in the cronus-forms database as **pending**: nothing is
checked yet. You get the usual email listing them. Write down the pilot start date (day 12
check-in, day 14 end).

## 2. They connect alerts, you activate

They add **@cronus_alerts_bot** to a Telegram group, or click *Add to Slack* on
cronussignals.com/connect. You get a "Telegram connected" or "Slack connected" email with
one link per client still waiting:

```
Whose alerts should go here? Click to review, then activate:
  bright-pixel--pilot  (3 endpoints, Bright Pixel, bo@brightpixel.co)
  https://cronussignals.com/api/admin/act?t=...
```

(If they connected first and sent the list after, the link is in the endpoint-list email
instead.) Click it. The page shows every URL and where alerts will go; nothing changes until
you press **Activate monitoring**.

Before you press it, check each URL is safe to call every 5 minutes. A checkout, payment or
form URL that creates a real order or lead when called must not be monitored: ask for a
health or read-only endpoint instead.

Links last 14 days. The forms can never change an endpoint that is already active, so a
stranger can't redirect a client's monitoring.

## 3. Optional: extra settings

Activated endpoints use sensible defaults. To add `expected_status`, `timezone`,
`severity`, `runbook_url`, `dashboard_url`, content checks or report branding, edit the
endpoint's `config` JSON in Cloudflare → D1 → cronus-onboarding → Console, for example:

```sql
update endpoints set config = '{"expected_status":[200,405],"timezone":"America/Chicago","severity":"critical"}'
where client_id = 'bright-pixel--pilot' and endpoint_name = 'shop.co/webhooks/orders';
```

Field names are the same as in `endpoints.schema.json`. Sentinel reads the change on its next run.

Clients can still be listed in the `ENDPOINTS_JSON` secret instead (that's where
`cronus-internal` lives). Both are monitored; if the same client_id + endpoint name is in
both, the secret's copy wins.

## 4. Confirm the URLs are healthy now

Wait for the next run (within 5 minutes), then Actions → Cronus Sentinel → latest run.
The `cronus-config` notice should count the new endpoints ("... + N from the client list"),
and in the `cronus-summary` line `failing` should be `[]`. If a URL is failing on day one,
fix the config (usually `expected_status`) before the client sees an alert.

If `blocked` lists an endpoint, the client's bot protection (usually Cloudflare) answered
with a challenge page instead of their app, so Cronus can't see its health. Fixes, easiest first:
- Point the check at a URL that bot protection doesn't cover (an API or webhook path, or a
  health-check URL on a subdomain that isn't proxied).
- Cloudflare Pro or higher: a WAF custom rule that skips bot checks for the Cronus user agent.
- Cloudflare Free: Bot Fight Mode can't be bypassed per request; the client would have to turn
  it off (Security → Bots) or use one of the options above.

## 5. Send the test alert (the client watches it arrive)

Actions → Cronus Sentinel → **Run workflow** → tick `test_alert`, type the new client's
`client_id` in `test_client` → Run. Only that client's channel(s) get one sample card marked
TEST; existing clients get nothing. (Leave `test_client` blank to send to every client,
e.g. after rotating the bot token.) The run's annotation says
`N/N test alert(s) delivered (one client)`. If anything failed, stop and fix it before go-live.

## 6. Tell the client, in writing

- What's watched (list the names), how often (every 5 minutes), and where alerts go.
- An alert means two failed checks 20 seconds apart. It is not an uptime guarantee.
- Pilot ends on day 14 (date). Link to the pilot terms: https://cronussignals.com/pilot-terms

## 7. Day 12 and day 14

- Day 12: run the workflow with **weekly_report** ticked (and `dry_run` unticked) so the client
  gets a fresh report in their channel, then send the payment link with a line pointing to it.
- Day 14: paid → keep running. Not paid → pause them the same day and tell them it's off.
  Never keep monitoring a client who hasn't agreed to pay. To pause: the **pause later** link
  on the page you saw after activating, or the cancellation email's link for paid clients, or
  in the D1 console: `update endpoints set status = 'paused' where client_id = '...';`

## Offboarding

Remove their entries from `ENDPOINTS_JSON`, confirm `n` dropped in the next run's summary,
and ask them to remove the bot from their group.

## Paid clients (automatic)

The `cronus-forms` Worker (`forms/worker.js`) listens to Stripe at `/api/stripe/webhook` and emails
cronussignals@gmail.com:

- **New Pro Agency client** when someone pays. Stripe has already sent them to /endpoints; their
  list arrives as a separate "Endpoint list" email. Continue from step 3.
- **No endpoint list yet**, once, if nothing has arrived from their paying email after 48 hours.
- **Payment failed** on a renewal. Stripe retries and emails them; nothing to do yet.
- **Cancelled** when the subscription ends. Do the offboarding steps below that day.

Records live in the D1 database `cronus-onboarding` (`forms/schema.sql`). Tests: `cd forms && node test.mjs`.
