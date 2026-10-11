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
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    severity: str = ""                        # optional label shown in the header, e.g. "critical"
    timezone: str = ""                        # optional IANA zone for local times, e.g. "America/Chicago"
    runbook_url: str = ""                     # optional; becomes a button on the alert
    dashboard_url: str = ""                   # optional; becomes a button on the alert

    @property
    def key(self) -> str:
        """Stable identity used for the state file."""
        return f"{self.client_id}::{self.endpoint_name}"


@dataclass
class ProbeResult:
    ok: bool
    status: int | None          # None = no HTTP response at all
    latency_ms: int
    error: str = ""             # "", "timeout", "dns", "tls", "refused", "disconnected", "connection"
    checked_at: str = ""
    blocked: bool = False       # a bot-protection challenge answered instead of the app
    # Evidence for the alert card (filled on failures only; never written to the public log)
    reason: str = ""            # HTTP reason phrase, e.g. "Internal Server Error"
    error_detail: str = ""      # low-level connection error text
    served_by: str = ""         # hosting/CDN detected from response headers
    request_id: str = ""        # "<header> <value>" the client can search their logs for
    platform_error: str = ""    # e.g. Vercel's x-vercel-error code
    body_excerpt: str = ""      # first line of the response body, redacted and trimmed
    final_url: str = ""         # set when redirects led somewhere other than target_url


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
        tz = str(clean.get("timezone") or "")
        if tz:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                problems.append(f"endpoint #{i}: unknown timezone (use an IANA name like America/Chicago)")
                continue
        bad_link = [k for k in ("runbook_url", "dashboard_url")
                    if clean.get(k) and not str(clean[k]).startswith("https://")]
        if bad_link:
            problems.append(f"endpoint #{i}: {', '.join(bad_link)} must start with https://")
            continue
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


TS_FORMAT = "%Y-%m-%d %H:%M:%S UTC"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(ts: str | None) -> datetime | None:
    """Inverse of utc_now(); None for missing or unreadable values."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_duration(delta: timedelta) -> str:
    """'45 s', '7 min', '2 h 05 min', '1 d 3 h'."""
    s = max(0, int(delta.total_seconds()))
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def fmt_when(dt: datetime | None, tz_name: str = "", ref: datetime | None = None) -> str:
    """'12:52 UTC' (+ 'Oct 11' when not today) and the client's local time if configured."""
    if dt is None:
        return "—"
    ref = ref or datetime.now(timezone.utc)
    day = "" if dt.date() == ref.date() else dt.strftime("%b %d ").replace(" 0", " ")
    out = f"{day}{dt:%H:%M} UTC"
    if tz_name:
        local = dt.astimezone(ZoneInfo(tz_name))
        out += f" · {local:%I:%M %p}".replace(" 0", " ") + f" {local:%Z}"
    return out


CLOUDFLARE_NAMES = {520: "Web Server Returned an Unknown Error", 521: "Web Server Is Down",
                    522: "Connection Timed Out", 523: "Origin Is Unreachable",
                    524: "A Timeout Occurred", 525: "SSL Handshake Failed",
                    526: "Invalid SSL Certificate", 530: "Origin DNS Error"}


def status_text(r: "ProbeResult") -> str:
    """'500 Internal Server Error', 'NO RESPONSE (dns)', '200 OK, but too slow'."""
    if r.status is None:
        return f"NO RESPONSE ({r.error})" if r.error else "NO RESPONSE"
    reason = r.reason if r.reason and r.reason.lower() != "unknown" else ""
    if not reason:
        try:
            reason = HTTPStatus(r.status).phrase
        except ValueError:
            reason = CLOUDFLARE_NAMES.get(r.status, "")
    text = f"{r.status} {reason}".strip()
    if r.error == "timeout":
        text += ", over budget"
    return text


# --------------------------------------------------------------------------- #
# Evidence from a failed response
# --------------------------------------------------------------------------- #
# Headers that carry an ID the client can paste into their own log search, most specific first.
REQUEST_ID_HEADERS = ("x-request-id", "x-correlation-id", "x-vercel-id", "fly-request-id",
                      "x-nf-request-id", "rndr-id", "x-amzn-requestid", "x-amz-cf-id", "cf-ray")

SECRET_RE = re.compile(
    r"""(?ix)
    ( ["']?(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|authorization|session)["']?
      \s*[:=]\s*["']? )
    [^"',\s}&]+""")
BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")


def detect_platform(h: Any) -> str:
    """Name the hosting/CDN layers visible in the response headers, outermost first."""
    found = []
    server = h.get("server", "").strip()
    if "cf-ray" in h or server.lower() == "cloudflare":
        found.append("Cloudflare")
    if "x-amz-cf-id" in h:
        found.append("CloudFront")
    if "x-vercel-id" in h or server.lower() == "vercel":
        found.append("Vercel")
    if "x-nf-request-id" in h or server.lower() == "netlify":
        found.append("Netlify")
    if "fly-request-id" in h:
        found.append("Fly.io")
    if "rndr-id" in h:
        found.append("Render")
    if "vegur" in h.get("via", "").lower():
        found.append("Heroku")
    if "x-shopid" in h or "x-shopify-stage" in h:
        found.append("Shopify")
    if not found and server:
        found.append(server[:40])
    return " → ".join(found)


def find_request_id(h: Any) -> str:
    for name in REQUEST_ID_HEADERS:
        if h.get(name):
            return f"{name} {h[name][:64]}"
    return ""


def body_excerpt(body: bytes, content_type: str, limit: int = 160) -> str:
    """A short, single-line, secret-redacted look at what the server said."""
    if not body:
        return ""
    text = body[:8192].decode("utf-8", "replace")
    if "html" in content_type.lower() or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        text = f"page title: {html.unescape(m.group(1))}" if m else re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    text = BEARER_RE.sub(r"\1 [redacted]", SECRET_RE.sub(r"\1[redacted]", text))
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
                body = await resp.read()  # full body counts toward latency
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
                error = ""
                if latency > ep.timeout_ms:
                    ok, error = False, "timeout"
                result = ProbeResult(ok, status, latency, error, utc_now(), reason=resp.reason or "")
                if not ok:
                    h = resp.headers
                    result.served_by = detect_platform(h)
                    result.request_id = find_request_id(h)
                    result.platform_error = h.get("x-vercel-error", "")[:60]
                    result.body_excerpt = body_excerpt(body, h.get("content-type", ""))
                    final = str(resp.url)
                    if final.rstrip("/") != ep.target_url.rstrip("/"):
                        result.final_url = final
                return result
        except asyncio.TimeoutError:
            latency = int((time.perf_counter() - start) * 1000)
            return ProbeResult(False, None, latency, "timeout", utc_now())
        except aiohttp.ClientError as exc:
            latency = int((time.perf_counter() - start) * 1000)
            return ProbeResult(False, None, latency, classify_error(exc), utc_now(),
                               error_detail=error_text(exc))


def classify_error(exc: BaseException) -> str:
    """Turn an aiohttp exception into a kind the alert can give specific advice for."""
    dns_error = getattr(aiohttp, "ClientConnectorDNSError", ())  # aiohttp >= 3.10
    if isinstance(exc, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError)):
        return "tls"
    if dns_error and isinstance(exc, dns_error):
        return "dns"
    if isinstance(exc, aiohttp.ClientConnectorError):
        os_err = getattr(exc, "os_error", None)
        if isinstance(os_err, OSError) and "name" in str(os_err).lower() and "resol" in str(os_err).lower():
            return "dns"
        return "refused"
    if isinstance(exc, (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError)):
        return "disconnected"
    return "connection"


def error_text(exc: BaseException) -> str:
    """The most useful line of a connection error, e.g. 'certificate has expired'."""
    inner = getattr(exc, "os_error", None) or getattr(exc, "certificate_error", None) or exc
    text = getattr(inner, "reason", None) or getattr(inner, "strerror", None) or str(inner)
    text = " ".join(str(text).split()) or type(exc).__name__
    return text[:120]


CLOUDFLARE_5XX = {
    520: "Your server sent Cloudflare an empty or malformed response. Check that the app process "
         "isn't crashing mid-request, and look for oversized response headers.",
    521: "Your server refused Cloudflare's connection. The web server is down or a firewall is "
         "blocking Cloudflare's IP ranges.",
    522: "Cloudflare timed out connecting to your server. The host is overloaded, offline, or "
         "dropping Cloudflare's IPs at the firewall.",
    523: "Cloudflare can't reach your server's address. Check the origin's DNS record and routing.",
    524: "Your server accepted the connection but took over 100 s to answer. Look for a stuck "
         "query or long-running job in this request path.",
    525: "TLS handshake between Cloudflare and your server failed. Check the origin certificate "
         "and the SSL mode in Cloudflare.",
    526: "Your server's certificate is invalid for Cloudflare's Full (strict) mode. Renew or "
         "replace the origin certificate.",
    530: "Cloudflare can't route to your origin (often a stopped Tunnel or a DNS error). "
         "Check the tunnel and the origin DNS record.",
}


def diagnose(r: ProbeResult) -> tuple[str, str]:
    """(where it broke, next step). 'where' is empty when the evidence doesn't say."""
    s = r.status or 0
    if "Cloudflare" in r.served_by and s in CLOUDFLARE_5XX:
        return "Between Cloudflare and your server (Cloudflare itself is up)", CLOUDFLARE_5XX[s]
    if r.platform_error:
        where = f"Vercel platform: {r.platform_error}"
        if "FUNCTION" in r.platform_error:
            return where, ("The serverless function failed or timed out. Open Vercel → Logs for "
                           "this request ID; check the latest deploy and function timeouts.")
        if "DEPLOYMENT" in r.platform_error:
            return where, "The deployment is missing or paused. Check the project's production deployment."
        return where, "Look up this error code in Vercel's docs and the request ID in Vercel → Logs."
    action = recommend_action(r)
    if r.request_id and (r.status or 0) >= 500:
        name, _, value = r.request_id.partition(" ")
        action += f" Search your logs for {name} {value} to find this exact request."
    return "", action


def recommend_action(r: ProbeResult) -> str:
    """Plain-language next step for the developer on call."""
    if r.error == "timeout":
        if r.status is not None:
            return (f"The endpoint answered but took {r.latency_ms} ms. Check slow queries, "
                    "third-party API calls in this path, and recent traffic spikes.")
        return ("No response within the budget. Check whether the host is overloaded or hung, "
                "and upstream dependencies (DB, third-party APIs).")
    if r.error == "dns":
        return ("The domain didn't resolve. Check the DNS records and that the domain "
                "registration hasn't lapsed.")
    if r.error == "tls":
        return ("The TLS certificate failed verification (often expired or wrong hostname). "
                "Renew it or check the auto-renewal job.")
    if r.error == "refused":
        return ("The connection was refused or couldn't be made. Check that the host process is "
                "running and listening, and firewall/WAF rules.")
    if r.error == "disconnected":
        return ("The server closed the connection before finishing its answer. Look for crashes "
                "or restarts in the app logs at this time.")
    if r.error:
        return ("No HTTP response. Verify DNS records, SSL certificate expiry, "
                "firewall/WAF rules, and that the host process is running.")
    s = r.status or 0
    if s in (401, 403):
        return ("Access denied. A key, token or firewall rule changed. If this endpoint should be "
                "public to the monitor, allow the Cronus Sentinel user agent.")
    if s == 405:
        return ("Method not allowed. If this receiver normally answers 405 to the check, reply and "
                "we'll mark 405 as healthy; otherwise a deploy changed which methods it accepts.")
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
@dataclass
class Alert:
    ep: Endpoint
    kind: str                   # "incident", "reminder", "resolved", "test"
    text: str                   # Telegram HTML

    @property
    def is_incident(self) -> bool:
        return self.kind in ("incident", "reminder")


def impact(r: ProbeResult) -> tuple[str, str]:
    """(emoji, word) for the card header: is it down, or up but unhealthy?"""
    s = r.status
    if s is None or s >= 500 or s == 404:
        return "🔴", "DOWN"
    if r.error == "timeout":
        return "🟠", "SLOW"
    return "🟠", "DEGRADED"


def esc(text: str) -> str:
    """Escape for Telegram HTML (quotes don't need it, and leaving them keeps JSON readable)."""
    return html.escape(text, quote=False)


def footer(ep: Endpoint) -> str:
    return f"<i>Cronus Sentinel · {esc(ep.client_id)}</i>"


def format_incident(ep: Endpoint, r: ProbeResult, fails: int, st: dict[str, Any] | None = None,
                    reminder: bool = False, now: datetime | None = None) -> str:
    """
    Telegram HTML. Telegram doesn't allow custom colours, so the 'dark' look
    comes from <pre> blocks, which render as a dark monospace panel in the
    Telegram dark theme, plus a restrained emoji severity marker.

    Lines appear only when there is evidence for them, so the card never shows
    an empty or guessed field.
    """
    e, st = esc, st or {}
    now = now or datetime.now(timezone.utc)
    icon, word = impact(r)
    if reminder:
        word = f"STILL {word}"
    sev = f" · {e(ep.severity.upper())}" if ep.severity else ""
    since = parse_ts(st.get("down_since")) or parse_ts(r.checked_at)

    rows = [("STATUS", status_text(r))]
    if since:
        rows.append(("SINCE", f"{fmt_when(since, ep.timezone, now)} ({fmt_duration(now - since)})"))
    rows.append(("LAST OK", fmt_when(parse_ts(st.get("last_ok_at")), ep.timezone, now)
                 if st.get("last_ok_at") else "none since monitoring began"))
    rows.append(("FAILURES", f"{fails} consecutive checks"))
    rows.append(("LATENCY", f"{r.latency_ms} ms (budget {ep.timeout_ms} ms)"))
    if r.final_url:
        rows.append(("ENDED AT", r.final_url))
    if r.served_by:
        rows.append(("SERVED BY", r.served_by))
    if r.request_id:
        rows.append(("REQ ID", r.request_id))
    if r.error_detail:
        rows.append(("ERROR", r.error_detail))
    if r.body_excerpt:
        rows.append(("BODY", r.body_excerpt))
    panel = "\n".join(f"{k:<9} {e(v)}" for k, v in rows)

    where, action = diagnose(r)
    lines = [
        f"{icon} <b>{word}</b> · <b>{e(ep.endpoint_name)}</b>{sev}",
        f"<code>{e(ep.method)} {e(ep.target_url)}</code>",
        f"<pre>{panel}</pre>",
    ]
    if where:
        lines.append(f"<b>Where:</b> {e(where)}")
    lines.append(f"<b>Next step:</b> {e(action)}")
    lines.append(footer(ep))
    return "\n".join(lines)


def format_resolved(ep: Endpoint, r: ProbeResult, st: dict[str, Any],
                    now: datetime | None = None) -> str:
    """Recovery card: how long it was down and what the failure was, for the client's records."""
    e = esc
    now = now or datetime.now(timezone.utc)
    since = parse_ts(st.get("down_since"))
    up = parse_ts(r.checked_at) or now
    rows = [("STATUS", status_text(r)), ("LATENCY", f"{r.latency_ms} ms")]
    if since:
        rows.append(("DOWNTIME", f"~{fmt_duration(up - since)}"))
        rows.append(("FROM", fmt_when(since, ep.timezone, now)))
        rows.append(("TO", fmt_when(up, ep.timezone, now)))
    if st.get("failure_count"):
        rows.append(("FAILED", f"{st['failure_count']} checks"))
    if st.get("last_fail_status"):
        rows.append(("CAUSE", st["last_fail_status"]))
    panel = "\n".join(f"{k:<9} {e(str(v))}" for k, v in rows)
    return "\n".join([
        f"🟢 <b>RECOVERED</b> · <b>{e(ep.endpoint_name)}</b>",
        f"<code>{e(ep.method)} {e(ep.target_url)}</code>",
        f"<pre>{panel}</pre>",
        "<i>Downtime is measured between checks, so the real outage may be slightly shorter.</i>",
        footer(ep),
    ])


def link_buttons(ep: Endpoint) -> list[tuple[str, str]]:
    return [(t, u) for t, u in (("Runbook", ep.runbook_url), ("Dashboard", ep.dashboard_url)) if u]


def to_plain(html_text: str) -> str:
    """Convert the Telegram HTML card to Slack mrkdwn."""
    text = re.sub(r"</?pre>", "```", html_text)
    text = re.sub(r"</?b>", "*", text)
    text = re.sub(r"</?i>", "_", text)
    text = re.sub(r"</?code>", "`", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Slack treats < > & as control characters, so escape them again.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
TELEGRAM_MAX = 4096


async def send_telegram(session: aiohttp.ClientSession, chat_id: str, alert: Alert) -> bool:
    text = alert.text
    if len(text) > TELEGRAM_MAX:  # only a huge URL could do this; keep the card sendable
        text = text[: TELEGRAM_MAX - 1] + "…"
    if DRY_RUN:
        if VERBOSE:
            print(f"\n--- DRY RUN → Telegram chat {chat_id} [{alert.kind.upper()}] ---\n{text}")
            for t, u in link_buttons(alert.ep):
                print(f"[button] {t} → {u}")
            print()
        else:
            print(f"--- DRY RUN → Telegram: would send {alert.kind.upper()} "
                  "(set SENTINEL_VERBOSE_LOGS=1 to print it)")
        return True
    if not TELEGRAM_BOT_TOKEN:
        print("[alert] TELEGRAM_BOT_TOKEN not set — alert not sent", file=sys.stderr)
        return False
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True}
    buttons = link_buttons(alert.ep)
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u} for t, u in buttons]]}
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


def slack_text(alert: Alert) -> str:
    links = "  ".join(f"<{u}|{t}>" for t, u in link_buttons(alert.ep))
    return to_plain(alert.text) + (f"\n{links}" if links else "")


async def send_slack(session: aiohttp.ClientSession, webhook: str, alert: Alert) -> bool:
    if not webhook:
        return True
    if DRY_RUN:
        if VERBOSE:
            print(f"--- DRY RUN → Slack [{alert.kind.upper()}] ---\n{slack_text(alert)}\n")
        return True
    try:
        async with session.post(webhook, json={"text": slack_text(alert)},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def dispatch(session: aiohttp.ClientSession, alert: Alert) -> bool:
    ep = alert.ep
    tg, sl = await asyncio.gather(send_telegram(session, ep.telegram_chat_id, alert),
                                  send_slack(session, ep.slack_webhook_url, alert))
    return tg and sl


# --------------------------------------------------------------------------- #
# Test alert (TEST_ALERT=1): proves delivery end to end without touching state
# --------------------------------------------------------------------------- #
def format_test(ep: Endpoint) -> str:
    """A fully filled sample card, so the client sees everything a real alert can carry."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sample = ProbeResult(
        ok=False, status=500, latency_ms=412, checked_at=now.strftime(TS_FORMAT),
        reason="Internal Server Error", served_by="Cloudflare",
        request_id="x-request-id 4f9c2e71-sample",
        body_excerpt='{"error":"database connection refused"} (sample)')
    st = {"down_since": (now - timedelta(minutes=3)).strftime(TS_FORMAT),
          "last_ok_at": (now - timedelta(minutes=8)).strftime(TS_FORMAT)}
    return ("🧪 <b>TEST ALERT</b> · nothing is down. This is sample data showing what a real "
            "incident card looks like:\n\n" + format_incident(ep, sample, 2, st, now=now))


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
        sent = await asyncio.gather(*(dispatch(session, Alert(ep, "test", format_test(ep)))
                                      for ep in targets))
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
        alerts: list[Alert] = []
        for ep in endpoints:
            st = state.setdefault(ep.key, {
                "failure_count": ep.failure_count, "incident_open": False,
                "down_since": None, "last_alert_at_count": 0,
            })
            for r in results[ep.key]:
                if r.ok:
                    if st["incident_open"]:
                        alerts.append(Alert(ep, "resolved", format_resolved(ep, r, dict(st))))
                        resolved.append(idx[ep.key])
                    st.update(failure_count=0, incident_open=False, down_since=None,
                              last_alert_at_count=0, last_fail_status=None)
                    if not r.blocked:  # a challenge page says nothing about health
                        st["last_ok_at"] = r.checked_at
                    continue

                st["failure_count"] += 1
                st["last_fail_status"] = status_text(r)
                st["down_since"] = st["down_since"] or r.checked_at
                n = st["failure_count"]
                opening = not st["incident_open"] and n >= ep.failure_threshold
                reminder = (st["incident_open"] and REMINDER_EVERY_N > 0
                            and n - st["last_alert_at_count"] >= REMINDER_EVERY_N)
                if opening or reminder:
                    kind = "incident" if opening else "reminder"
                    alerts.append(Alert(ep, kind, format_incident(ep, r, n, st, reminder=not opening)))
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
            sent = list(await asyncio.gather(*(dispatch(session, a) for a in alerts)))
            print(f"[sentinel] alerts: {sum(sent)}/{len(alerts)} delivered")
            # If an incident alert didn't go out, re-open the gate so the next
            # run tries again instead of silently swallowing the outage.
            for a, ok in zip(alerts, sent):
                if not ok and a.is_incident and state.get(a.ep.key, {}).get("incident_open"):
                    state[a.ep.key]["incident_open"] = False
                    state[a.ep.key]["last_alert_at_count"] = 0

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
