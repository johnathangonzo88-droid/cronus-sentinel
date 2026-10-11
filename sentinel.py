#!/usr/bin/env python3
"""
Cronus Sentinel — headless endpoint & webhook-receiver outage monitor.

What one run does
-----------------
1. Loads endpoint config from the ENDPOINTS_JSON env var (preferred: a GitHub
   Secret, so client URLs never sit in a public repo) or from endpoints.json.
2. Loads failure counters from .sentinel/state.json (persisted between runs by
   the workflow's actions/cache step — GitHub runners are wiped after each job).
3. Probes every endpoint concurrently with aiohttp. A probe FAILS on:
     - HTTP 5xx, HTTP 404 (or any status not in `expected_status`, if set)
     - connection errors (DNS, TLS, refused)
     - total latency > timeout_ms (default 3000 ms)
4. Endpoints that failed are re-probed once after CONFIRM_DELAY_S seconds
   (default 20). So "2 consecutive failures" can be confirmed inside ONE run,
   instead of waiting for the next cron tick (which GitHub may delay).
5. When an endpoint's consecutive-failure count reaches its threshold (default 2)
   an INCIDENT alert goes to that client's Telegram chat (and Slack, if set).
   Repeat alerts are suppressed while the incident stays open; a RESOLVED
   alert is sent on the first healthy probe afterwards.
6. Writes updated counters back to .sentinel/state.json.

Environment variables
---------------------
TELEGRAM_BOT_TOKEN   required to send Telegram alerts (from @BotFather)
ENDPOINTS_JSON       optional; full endpoints.json content as a string
ENDPOINTS_FILE       optional; path to config file (default: endpoints.json)
STATE_FILE           optional; default .sentinel/state.json
CONFIRM_DELAY_S      optional; seconds before re-probing failures (default 20, 0 = off)
REMINDER_EVERY_N     optional; re-alert every N failed checks while down (default 0 = never)
DRY_RUN              optional; "1" prints alerts instead of sending them
MAX_CONCURRENCY      optional; parallel probes (default 50)
SENTINEL_VERBOSE_LOGS optional; "1" prints client names, URLs and alert text
                     to the log. Leave OFF on GitHub: logs of a public repo
                     are public, so by default endpoints appear as #index only.

Exit codes: 0 = run completed (client outages are NOT a workflow failure),
            2 = configuration error (bad JSON, missing fields).

Local test:   pip install -r requirements.txt
              DRY_RUN=1 python sentinel.py
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

# --------------------------------------------------------------------------- #
# Settings (all overridable from the environment)
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ENDPOINTS_FILE = Path(os.getenv("ENDPOINTS_FILE", "endpoints.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sentinel/state.json"))
CONFIRM_DELAY_S = float(os.getenv("CONFIRM_DELAY_S", "20"))
REMINDER_EVERY_N = int(os.getenv("REMINDER_EVERY_N", "0"))
DRY_RUN = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}
VERBOSE = os.getenv("SENTINEL_VERBOSE_LOGS", "").lower() in {"1", "true", "yes"}
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "50"))
TEST_ALERT = os.getenv("TEST_ALERT", "").lower() in {"1", "true", "yes"}

DEFAULT_TIMEOUT_MS = 3000
DEFAULT_THRESHOLD = 2
USER_AGENT = "CronusSentinel/1.0 (+https://cronussignals.com)"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Endpoint:
    client_id: str
    endpoint_name: str
    target_url: str
    telegram_chat_id: str
    failure_count: int = 0                    # seed value; live count is in state
    failure_threshold: int = DEFAULT_THRESHOLD
    method: str = "GET"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    expected_status: list[int] = field(default_factory=list)  # empty = "not 404, not 5xx"
    slack_webhook_url: str = ""
    enabled: bool = True

    @property
    def key(self) -> str:
        """Stable identity used for the state file."""
        return f"{self.client_id}::{self.endpoint_name}"


@dataclass
class ProbeResult:
    ok: bool
    status: int | None          # None = no HTTP response at all
    latency_ms: int
    error: str = ""             # "timeout", "connection: ...", or ""
    checked_at: str = ""
    blocked: bool = False       # a bot-protection challenge answered instead of the app


REQUIRED = ("client_id", "endpoint_name", "target_url", "telegram_chat_id")


def load_endpoints() -> list[Endpoint]:
    """Read config from ENDPOINTS_JSON (secret) or the endpoints file."""
    raw = os.getenv("ENDPOINTS_JSON", "").strip()
    source = "ENDPOINTS_JSON"
    if not raw:
        if not ENDPOINTS_FILE.exists():
            sys.exit(f"[config] no ENDPOINTS_JSON and {ENDPOINTS_FILE} not found")
        raw = ENDPOINTS_FILE.read_text(encoding="utf-8")
        source = str(ENDPOINTS_FILE)

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[config] invalid JSON in {source}: {exc}", file=sys.stderr)
        sys.exit(2)

    items = doc.get("endpoints", []) if isinstance(doc, dict) else doc
    endpoints, problems = [], []
    allowed = set(Endpoint.__dataclass_fields__)
    for i, item in enumerate(items):
        missing = [k for k in REQUIRED if not item.get(k)]
        if missing:
            problems.append(f"endpoint #{i}: missing {', '.join(missing)}")
            continue
        url = str(item["target_url"])
        if not url.startswith(("http://", "https://")):
            problems.append(f"endpoint #{i}: target_url must start with http(s)://")
            continue
        clean = {k: v for k, v in item.items() if k in allowed}
        clean["telegram_chat_id"] = str(clean["telegram_chat_id"])
        clean["method"] = str(clean.get("method", "GET")).upper()
        endpoints.append(Endpoint(**clean))

    if problems:
        print("[config] " + "\n[config] ".join(problems), file=sys.stderr)
        sys.exit(2)

    keys = [e.key for e in endpoints]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        shown = sorted(dupes) if VERBOSE else f"{len(dupes)} duplicate key(s)"
        print(f"[config] duplicate client_id+endpoint_name: {shown}", file=sys.stderr)
        sys.exit(2)

    return [e for e in endpoints if e.enabled]


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[state] corrupt state file — starting fresh", file=sys.stderr)
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomic swap so a crash never leaves half a file


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def keys_hash(keys: list[str]) -> str:
    """Identifies the endpoint list; Jarvis's ops_export_endpoints computes the same."""
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:12]


def label(ep: "Endpoint", idx: dict[str, int]) -> str:
    """How an endpoint appears in the log: its name only when VERBOSE."""
    return ep.key if VERBOSE else f"#{idx[ep.key]}"


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
async def probe(session: aiohttp.ClientSession, ep: Endpoint, sem: asyncio.Semaphore) -> ProbeResult:
    """One HTTP check. Never raises — every outcome becomes a ProbeResult."""
    timeout = aiohttp.ClientTimeout(total=ep.timeout_ms / 1000)
    async with sem:
        start = time.perf_counter()
        try:
            async with session.request(ep.method, ep.target_url, timeout=timeout,
                                       allow_redirects=True) as resp:
                await resp.read()  # full body counts toward latency
                latency = int((time.perf_counter() - start) * 1000)
                status = resp.status
                # Cloudflare bot protection answers with a challenge page instead of
                # the app. That says nothing about the endpoint's health, so it is not
                # an outage alert; it is reported separately so the owner can fix access.
                if resp.headers.get("cf-mitigated", "").lower() == "challenge":
                    return ProbeResult(True, status, latency, "blocked by bot protection",
                                       utc_now(), blocked=True)
                if ep.expected_status:
                    ok = status in ep.expected_status
                else:
                    ok = status != 404 and status < 500
                # Slow-but-successful still counts as a failure past the budget.
                if latency > ep.timeout_ms:
                    return ProbeResult(False, status, latency, "timeout", utc_now())
                return ProbeResult(ok, status, latency, "", utc_now())
        except asyncio.TimeoutError:
            latency = int((time.perf_counter() - start) * 1000)
            return ProbeResult(False, None, latency, "timeout", utc_now())
        except aiohttp.ClientError as exc:
            latency = int((time.perf_counter() - start) * 1000)
            return ProbeResult(False, None, latency, f"connection: {type(exc).__name__}", utc_now())


def recommend_action(r: ProbeResult) -> str:
    """Plain-language next step for the developer on call."""
    if r.error == "timeout":
        return ("Response exceeded the latency budget. Check upstream dependencies "
                "(DB, third-party APIs), cold starts, and recent traffic spikes.")
    if r.error.startswith("connection"):
        return ("No HTTP response. Verify DNS records, SSL certificate expiry, "
                "firewall/WAF rules, and that the host process is running.")
    s = r.status or 0
    if s == 404:
        return ("Route not found. Confirm the latest deploy didn't rename or drop "
                "this path, and that webhook URLs in Shopify/Stripe still match.")
    if s == 429:
        return "Rate-limited. Allowlist the monitor or raise the limit for this path."
    if s == 502:
        return "Bad gateway. The proxy/load balancer can't reach the app — check app health and upstream config."
    if s == 503:
        return "Service unavailable. Check for maintenance mode, crashed workers, or exhausted capacity."
    if s == 504:
        return "Gateway timeout. The app is reachable but too slow — inspect slow queries and worker saturation."
    if s >= 500:
        return "Server error. Pull application logs for this timestamp and review the most recent deploy."
    return f"Unexpected status {s}. Compare against the endpoint's expected response."


# --------------------------------------------------------------------------- #
# Alert formatting
# --------------------------------------------------------------------------- #
def format_incident(ep: Endpoint, r: ProbeResult, fails: int) -> str:
    """
    Telegram HTML. Telegram doesn't allow custom colours, so the 'dark' look
    comes from <pre> blocks, which render as a dark monospace panel in the
    Telegram dark theme, plus a restrained emoji severity marker.
    """
    e = html.escape
    status = str(r.status) if r.status is not None else "NO RESPONSE"
    reason = f" ({r.error})" if r.error else ""
    return (
        "🔴 <b>CRONUS SENTINEL · INCIDENT</b>\n"
        f"<b>{e(ep.endpoint_name)}</b> · <code>{e(ep.client_id)}</code>\n"
        "<pre>"
        f"TARGET    {e(ep.endpoint_name)}\n"
        f"URL       {e(ep.target_url)}\n"
        f"STATUS    {e(status + reason)}\n"
        f"LATENCY   {r.latency_ms} ms (budget {ep.timeout_ms} ms)\n"
        f"FAILURES  {fails} consecutive\n"
        f"TIME      {e(r.checked_at)}"
        "</pre>\n"
        f"<b>Recommended action:</b> {e(recommend_action(r))}"
    )


def format_resolved(ep: Endpoint, r: ProbeResult, down_since: str) -> str:
    e = html.escape
    return (
        "🟢 <b>CRONUS SENTINEL · RESOLVED</b>\n"
        f"<b>{e(ep.endpoint_name)}</b> · <code>{e(ep.client_id)}</code>\n"
        "<pre>"
        f"URL       {e(ep.target_url)}\n"
        f"STATUS    {r.status}\n"
        f"LATENCY   {r.latency_ms} ms\n"
        f"DOWN FROM {e(down_since)}\n"
        f"UP AT     {e(r.checked_at)}"
        "</pre>"
    )


def to_plain(html_text: str) -> str:
    """Strip tags for Slack (Slack uses mrkdwn, not HTML)."""
    import re
    text = re.sub(r"</?pre>", "```", html_text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
async def send_telegram(session: aiohttp.ClientSession, chat_id: str, text: str) -> bool:
    if DRY_RUN:
        if VERBOSE:
            print(f"\n--- DRY RUN → Telegram chat {chat_id} ---\n{text}\n")
        else:
            kind = "RESOLVED" if "RESOLVED" in text else "INCIDENT"
            print(f"--- DRY RUN → Telegram: would send {kind} (set SENTINEL_VERBOSE_LOGS=1 to print it)")
        return True
    if not TELEGRAM_BOT_TOKEN:
        print("[alert] TELEGRAM_BOT_TOKEN not set — alert not sent", file=sys.stderr)
        return False
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for attempt in range(3):  # Telegram occasionally 429s or blips
        try:
            async with session.post(TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN),
                                    json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                if resp.status == 429:
                    retry = (await resp.json(content_type=None)).get("parameters", {}).get("retry_after", 2)
                    await asyncio.sleep(min(float(retry), 10))
                    continue
                # Never print the token; the body is safe to log.
                where = chat_id if VERBOSE else "<chat hidden>"
                print(f"[alert] Telegram {resp.status} for {where}: {body[:300]}", file=sys.stderr)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"[alert] Telegram send error ({type(exc).__name__}), attempt {attempt + 1}", file=sys.stderr)
            await asyncio.sleep(2)
    return False


async def send_slack(session: aiohttp.ClientSession, webhook: str, text: str) -> bool:
    if not webhook:
        return True
    if DRY_RUN:
        if VERBOSE:
            print(f"--- DRY RUN → Slack ---\n{to_plain(text)}\n")
        return True
    try:
        async with session.post(webhook, json={"text": to_plain(text)},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def dispatch(session: aiohttp.ClientSession, ep: Endpoint, text: str) -> bool:
    tg, sl = await asyncio.gather(send_telegram(session, ep.telegram_chat_id, text),
                                  send_slack(session, ep.slack_webhook_url, text))
    return tg and sl


# --------------------------------------------------------------------------- #
# Test alert (TEST_ALERT=1): proves delivery end to end without touching state
# --------------------------------------------------------------------------- #
def format_test(ep: Endpoint) -> str:
    sample = ProbeResult(ok=False, status=500, latency_ms=412, checked_at=utc_now())
    return ("🧪 <b>TEST ALERT</b> · nothing is down. A real incident card looks like this:\n\n"
            + format_incident(ep, sample, 2))


async def send_test_alerts(endpoints: list[Endpoint]) -> int:
    """One sample card per alert destination (Telegram chat / Slack webhook)."""
    seen: set[tuple[str, str]] = set()
    targets = []
    for ep in endpoints:
        dest = (ep.telegram_chat_id, ep.slack_webhook_url)
        if dest not in seen:
            seen.add(dest)
            targets.append(ep)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        sent = await asyncio.gather(*(dispatch(session, ep, format_test(ep)) for ep in targets))
    ok = sum(sent)
    line = f"{ok}/{len(targets)} test alert(s) delivered" + (" (dry run)" if DRY_RUN else "")
    if IN_ACTIONS:
        print(f"::notice title=cronus-test-alert::{line}")
        if ok < len(targets):
            print(f"::error title=cronus-test-alert::{len(targets) - ok} test alert(s) failed")
    else:
        print(f"[test] {line}")
    return 0 if ok == len(targets) else 1


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #
async def run() -> int:
    endpoints = load_endpoints()
    state = load_state()
    if not endpoints:
        print("[sentinel] no enabled endpoints — nothing to do")
        return 0

    if TEST_ALERT:
        return await send_test_alerts(endpoints)

    idx = {ep.key: i for i, ep in enumerate(endpoints)}
    cfg_hash = keys_hash([ep.key for ep in endpoints])
    opened: list[int] = []
    resolved: list[int] = []
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    headers = {"User-Agent": USER_AGENT}
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENCY, ttl_dns_cache=300)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # Pass 1 — everything in parallel.
        first = await asyncio.gather(*(probe(session, ep, sem) for ep in endpoints))
        results: dict[str, list[ProbeResult]] = {ep.key: [r] for ep, r in zip(endpoints, first)}

        # Pass 2 — re-probe only the failures, after a short pause.
        failed = [ep for ep, r in zip(endpoints, first) if not r.ok]
        if failed and CONFIRM_DELAY_S > 0:
            print(f"[sentinel] {len(failed)} failed; confirming in {CONFIRM_DELAY_S:.0f}s")
            await asyncio.sleep(CONFIRM_DELAY_S)
            second = await asyncio.gather(*(probe(session, ep, sem) for ep in failed))
            for ep, r in zip(failed, second):
                results[ep.key].append(r)

        # Apply results in order to each endpoint's counter, alert on transitions.
        alerts = []
        for ep in endpoints:
            st = state.setdefault(ep.key, {
                "failure_count": ep.failure_count, "incident_open": False,
                "down_since": None, "last_alert_at_count": 0,
            })
            for r in results[ep.key]:
                if r.ok:
                    if st["incident_open"]:
                        alerts.append((ep, format_resolved(ep, r, st["down_since"] or "unknown")))
                        resolved.append(idx[ep.key])
                    st.update(failure_count=0, incident_open=False,
                              down_since=None, last_alert_at_count=0)
                    continue

                st["failure_count"] += 1
                st["down_since"] = st["down_since"] or r.checked_at
                n = st["failure_count"]
                opening = not st["incident_open"] and n >= ep.failure_threshold
                reminder = (st["incident_open"] and REMINDER_EVERY_N > 0
                            and n - st["last_alert_at_count"] >= REMINDER_EVERY_N)
                if opening or reminder:
                    alerts.append((ep, format_incident(ep, r, n)))
                    if opening:
                        opened.append(idx[ep.key])
                    st["incident_open"] = True
                    st["last_alert_at_count"] = n

            last = results[ep.key][-1]
            st.update(last_status=last.status, last_latency_ms=last.latency_ms,
                      last_checked=last.checked_at)
            mark = "BLKD" if last.blocked else ("OK  " if last.ok else "FAIL")
            print(f"[{mark}] {label(ep, idx):<40} status={last.status} "
                  f"latency={last.latency_ms}ms fails={st['failure_count']}")

        # Drop state for endpoints removed from config.
        live = {ep.key for ep in endpoints}
        for k in [k for k in state if k not in live]:
            del state[k]

        sent: list[bool] = []
        if alerts:
            sent = list(await asyncio.gather(*(dispatch(session, ep, txt) for ep, txt in alerts)))
            print(f"[sentinel] alerts: {sum(sent)}/{len(alerts)} delivered")
            # If an incident alert didn't go out, re-open the gate so the next
            # run tries again instead of silently swallowing the outage.
            for (ep, txt), ok in zip(alerts, sent):
                if not ok and "INCIDENT" in txt and state.get(ep.key, {}).get("incident_open"):
                    state[ep.key]["incident_open"] = False
                    state[ep.key]["last_alert_at_count"] = 0

        # One machine-readable line per run for Jarvis's Ledger (Protocol 8).
        # Counts and endpoint indexes only — the log of a public repo is public;
        # Jarvis maps indexes back to names with the keys it exported (cfg).
        failing = [idx[ep.key] for ep in endpoints if not results[ep.key][-1].ok]
        blocked = [idx[ep.key] for ep in endpoints if results[ep.key][-1].blocked]
        summary = {
            "v": 1, "cfg": cfg_hash, "n": len(endpoints), "failing": failing, "blocked": blocked,
            "opened": opened, "resolved": resolved,
            "alerts_sent": sum(sent), "alerts_failed": len(sent) - sum(sent),
        }
        line = json.dumps(summary, separators=(",", ":"))
        if IN_ACTIONS:
            print(f"::notice title=cronus-summary::{line}")
            if blocked:
                print(f"::warning title=cronus-blocked::{len(blocked)} endpoint(s) answered with a bot-protection "
                      "challenge, so their health is unknown. Allow the monitor through (see ONBOARDING.md).")
            if summary["alerts_failed"]:
                print(f"::warning title=cronus-alert-failed::{summary['alerts_failed']} alert(s) failed to deliver")
        else:
            print(f"[summary] {line}")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
