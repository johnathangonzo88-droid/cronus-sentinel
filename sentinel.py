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
     - a content check, when configured: body_contains / body_not_contains,
       error_keywords, json_rules, min_body_bytes, or a sudden size drop
       (size_drop_pct). This catches "200 OK, but broken" responses.
   Heartbeat endpoints (kind = "heartbeat") work the other way round: a Zap,
   Make scenario or cron job pings https://cronussignals.com/api/hb/<id> on
   every run, and the endpoint FAILS when no ping arrived in expect_every_min.
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
HEARTBEAT_READ_TOKEN required for heartbeat endpoints and ENDPOINTS_URL; same value as
                     the cronus-forms Worker secret of that name
ENDPOINTS_URL        optional; the cronus-forms client list (GET /api/sentinel/endpoints).
                     Its active endpoints are monitored alongside ENDPOINTS_JSON. If it can't
                     be reached, the last good copy (kept in the state file) is used.
SLACK_DEAD_URL       optional; where a Slack webhook that Slack refused for good is reported
                     (cronus-forms emails the inbox once). Uses HEARTBEAT_READ_TOKEN.
SEND_MONTHLY         optional; "1" sends every client's branded monthly report now
REPORTS_DIR          optional; where dry runs write report HTML (default .sentinel/reports)
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
import ssl
import sys
import time
import urllib.error
import urllib.request
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


def dispatch_input(name: str) -> str:
    """A workflow_dispatch input, read from the event payload file.

    Values passed through a step's env: block are printed, unmasked, at the top of
    that step's log, and the logs of a public repo are public. Reading the input
    from $GITHUB_EVENT_PATH instead keeps a client_id out of the log entirely.
    """
    if os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch":
        return ""
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
            value = (json.load(fh).get("inputs") or {}).get(name)
    except (KeyError, OSError, ValueError, AttributeError):
        return ""
    return str(value or "").strip()


# Limit TEST_ALERT to one client_id (blank = everyone). Locally it comes from the
# environment; on GitHub, from the test_client input (see dispatch_input).
TEST_CLIENT = os.getenv("TEST_CLIENT", "").strip() or dispatch_input("test_client")
SEND_WEEKLY = os.getenv("SEND_WEEKLY", "").lower() in {"1", "true", "yes"}   # preview now, keep counters
WEEKLY_DAY = int(os.getenv("WEEKLY_DAY", "0"))              # 0 = Monday
WEEKLY_HOUR_UTC = int(os.getenv("WEEKLY_HOUR_UTC", "14"))   # 14 UTC = 9 AM Chicago (summer)
CERT_WARN_DAYS = sorted({int(x) for x in os.getenv("CERT_WARN_DAYS", "14,3").split(",") if x.strip()},
                        reverse=True)
CERT_CHECK_EVERY_H = float(os.getenv("CERT_CHECK_EVERY_H", "12"))
SEND_MONTHLY = os.getenv("SEND_MONTHLY", "").lower() in {"1", "true", "yes"}  # preview now, keep counters
MONTHLY_HOUR_UTC = int(os.getenv("MONTHLY_HOUR_UTC", os.getenv("WEEKLY_HOUR_UTC", "14")))
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", ".sentinel/reports"))
HEARTBEAT_STATUS_URL = os.getenv("HEARTBEAT_STATUS_URL", "https://cronussignals.com/api/hb/{id}/status")
HEARTBEAT_PING_URL = os.getenv("HEARTBEAT_PING_URL", "https://cronussignals.com/api/hb/{id}")
HEARTBEAT_READ_TOKEN = os.getenv("HEARTBEAT_READ_TOKEN", "").strip()
ENDPOINTS_URL = os.getenv("ENDPOINTS_URL", "").strip()
SLACK_DEAD_URL = os.getenv("SLACK_DEAD_URL", "https://cronussignals.com/api/sentinel/slack-dead")
SIZE_MIN_SAMPLES = int(os.getenv("SIZE_MIN_SAMPLES", "12"))   # ~1 hour of history before drift alerts
META_KEY = "__meta__"   # run-level state, kept alongside the per-endpoint entries

# Phrases that mean "the server answered, but the app behind it failed" (error_keywords: true).
ERROR_KEYWORDS = ("fatal error", "uncaught exception", "traceback (most recent call last)",
                  "stack trace", "internal server error", "something went wrong",
                  "service unavailable", "database error", "connection refused",
                  "upstream connect error")

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
    weekly_summary: bool = True               # include in the Monday report
    # Content checks (all optional; any one failing makes the check fail)
    body_contains: list[str] = field(default_factory=list)      # every phrase must appear
    body_not_contains: list[str] = field(default_factory=list)  # none may appear
    error_keywords: bool = False              # also fail on common app-error phrases
    json_rules: list[dict] = field(default_factory=list)        # [{"path": "data.items", "rule": "not_empty"}]
    min_body_bytes: int = 0                   # a smaller body is a failure
    size_drop_pct: int = 0                    # fail if the body shrinks this % below its usual size
    # Heartbeats: the client's automation pings us; silence is the failure
    kind: str = "http"                        # "http" or "heartbeat"
    heartbeat_id: str = ""                    # hb_<24 hex>, registered in the cronus-forms D1 table
    expect_every_min: int = 0                 # how often the automation normally runs
    # Branded monthly report (taken from the first endpoint of a client that sets them)
    monthly_report: bool = True
    client_name: str = ""                     # shown on the report; default client_id
    report_brand: str = ""                    # the agency's name, e.g. "Northwind Studio"
    report_color: str = ""                    # accent colour, e.g. "#1f6feb"
    report_logo_url: str = ""                 # https URL of the agency logo
    revenue_per_hour: float = 0               # optional; turns downtime into an estimate of sales at risk

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
    check_failed: str = ""      # which content/heartbeat check failed, in plain words
    body_bytes: int = 0         # response size, for size-drift tracking


REQUIRED = ("client_id", "endpoint_name", "target_url")   # plus a telegram_chat_id or slack_webhook_url
HEARTBEAT_ID_RE = re.compile(r"hb_[0-9a-f]{24}")
RULES = ("exists", "not_empty", "equals", "not_equals", "min", "max")


def validate_rules(rules: Any) -> str:
    """'' if json_rules is well formed, otherwise what's wrong with it."""
    if not isinstance(rules, list):
        return "json_rules must be a list"
    for n, rule in enumerate(rules):
        if not isinstance(rule, dict) or not str(rule.get("path", "")).strip():
            return f"json_rules[{n}] needs a \"path\""
        kind = rule.get("rule", "not_empty")
        if kind not in RULES:
            return f"json_rules[{n}]: rule must be one of {', '.join(RULES)}"
        if kind in ("equals", "not_equals", "min", "max") and "value" not in rule:
            return f"json_rules[{n}]: rule \"{kind}\" needs a \"value\""
        if kind in ("min", "max") and not isinstance(rule["value"], (int, float)):
            return f"json_rules[{n}]: \"{kind}\" needs a number"
    return ""


def parse_endpoints(items: Any) -> tuple[list[Endpoint], list[str]]:
    """Validate raw endpoint dicts. Problems name the item by index only (public logs)."""
    if not isinstance(items, list):
        return [], ["the endpoint list must be a JSON array (or {\"endpoints\": [...]})"]
    endpoints, problems = [], []
    allowed = set(Endpoint.__dataclass_fields__)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"endpoint #{i}: must be a JSON object")
            continue
        if str(item.get("kind", "http")) == "heartbeat":
            hb = str(item.get("heartbeat_id", ""))
            if not HEARTBEAT_ID_RE.fullmatch(hb):
                problems.append(f"endpoint #{i}: heartbeat_id must look like hb_ followed by 24 hex characters")
                continue
            if int(item.get("expect_every_min") or 0) < 5:
                problems.append(f"endpoint #{i}: expect_every_min must be 5 or more")
                continue
            item = {**item, "target_url": HEARTBEAT_STATUS_URL.format(id=hb), "method": "GET"}
        elif item.get("kind", "http") != "http":
            problems.append(f"endpoint #{i}: kind must be \"http\" or \"heartbeat\"")
            continue
        rule_problem = validate_rules(item.get("json_rules") or [])
        if rule_problem:
            problems.append(f"endpoint #{i}: {rule_problem}")
            continue
        missing = [k for k in REQUIRED if not item.get(k)]
        if missing:
            problems.append(f"endpoint #{i}: missing {', '.join(missing)}")
            continue
        if not item.get("telegram_chat_id") and not item.get("slack_webhook_url"):
            problems.append(f"endpoint #{i}: needs a telegram_chat_id or a slack_webhook_url")
            continue
        url = str(item["target_url"])
        if not url.startswith(("http://", "https://")):
            problems.append(f"endpoint #{i}: target_url must start with http(s)://")
            continue
        clean = {k: v for k, v in item.items() if k in allowed}
        clean["telegram_chat_id"] = str(clean.get("telegram_chat_id") or "")
        clean["method"] = str(clean.get("method", "GET")).upper()
        tz = str(clean.get("timezone") or "")
        if tz:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                problems.append(f"endpoint #{i}: unknown timezone (use an IANA name like America/Chicago)")
                continue
        bad_link = [k for k in ("runbook_url", "dashboard_url", "report_logo_url")
                    if clean.get(k) and not str(clean[k]).startswith("https://")]
        if bad_link:
            problems.append(f"endpoint #{i}: {', '.join(bad_link)} must start with https://")
            continue
        try:
            endpoints.append(Endpoint(**clean))
        except TypeError:
            problems.append(f"endpoint #{i}: a field has the wrong type")
    return endpoints, problems


def config_warning(msg: str) -> None:
    print(f"::warning title=cronus-config::{msg}" if IN_ACTIONS else f"[config] {msg}", file=sys.stdout if IN_ACTIONS else sys.stderr)


def fetch_remote_items() -> list[Any]:
    """GET the client list from the cronus-forms Worker. Raises on any failure."""
    req = urllib.request.Request(ENDPOINTS_URL, headers={
        "Authorization": f"Bearer {HEARTBEAT_READ_TOKEN}", "User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed https URL from config)
        doc = json.loads(resp.read(5_000_000).decode("utf-8"))
    items = doc.get("endpoints") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("response has no endpoints list")
    return items


def load_remote(state: dict[str, Any]) -> list[Endpoint]:
    """Endpoints from ENDPOINTS_URL, falling back to the last good copy in the state file.

    Bad rows are skipped with a warning instead of stopping the run: one malformed row
    in the database must never switch off monitoring for everyone else.
    """
    if not ENDPOINTS_URL:
        return []
    meta = state.setdefault(META_KEY, {})
    if not HEARTBEAT_READ_TOKEN:
        config_warning("ENDPOINTS_URL is set but HEARTBEAT_READ_TOKEN is not; the client list was not loaded")
        items = meta.get("remote_endpoints") or []
    else:
        try:
            items = fetch_remote_items()
            meta["remote_endpoints"] = items
            meta["remote_at"] = utc_now()
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            items = meta.get("remote_endpoints")
            if items is None:
                config_warning(f"client list unreachable ({type(exc).__name__}) and no saved copy yet; "
                               "only ENDPOINTS_JSON is monitored this run")
                return []
            config_warning(f"client list unreachable ({type(exc).__name__}); using the copy saved "
                           f"{meta.get('remote_at') or 'earlier'}")
    endpoints, problems = parse_endpoints(items)
    if problems:
        config_warning(f"{len(problems)} row(s) in the client list skipped as invalid: " + "; ".join(problems)[:500])
    return endpoints


def load_endpoints(state: dict[str, Any] | None = None) -> list[Endpoint]:
    """ENDPOINTS_JSON (secret) or the endpoints file, plus the client list from ENDPOINTS_URL."""
    raw = os.getenv("ENDPOINTS_JSON", "").strip()
    source = "ENDPOINTS_JSON"
    if not raw:
        if ENDPOINTS_URL:
            raw, source = "[]", "(none)"   # the example file is never monitored for real
        elif not ENDPOINTS_FILE.exists():
            sys.exit(f"[config] no ENDPOINTS_JSON and {ENDPOINTS_FILE} not found")
        else:
            raw = ENDPOINTS_FILE.read_text(encoding="utf-8")
            source = str(ENDPOINTS_FILE)

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[config] invalid JSON in {source}: {exc}", file=sys.stderr)
        sys.exit(2)

    items = doc.get("endpoints", []) if isinstance(doc, dict) else doc
    endpoints, problems = parse_endpoints(items)
    if problems:
        print("[config] " + "\n[config] ".join(problems), file=sys.stderr)
        sys.exit(2)

    keys = [e.key for e in endpoints]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        shown = sorted(dupes) if VERBOSE else f"{len(dupes)} duplicate key(s)"
        print(f"[config] duplicate client_id+endpoint_name: {shown}", file=sys.stderr)
        sys.exit(2)

    # The database list adds to the secret; on a clash the secret's entry wins.
    remote = load_remote(state if state is not None else {})
    seen, clashes, kept = set(keys), 0, []
    for ep in remote:
        if ep.key in seen:
            clashes += 1
            continue
        seen.add(ep.key)
        kept.append(ep)
    if clashes:
        config_warning(f"{clashes} client-list endpoint(s) also in ENDPOINTS_JSON; the ENDPOINTS_JSON entry is used")
    if ENDPOINTS_URL:
        line = f"{len(endpoints)} from ENDPOINTS_JSON + {len(kept)} from the client list"
        print(f"::notice title=cronus-config::{line}" if IN_ACTIONS else f"[config] {line}")

    return [e for e in endpoints + kept if e.enabled]


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
    """'500 Internal Server Error', 'NO RESPONSE (dns)', '200 OK, over budget'."""
    if r.error == "heartbeat":
        return "NO CHECK-IN"
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
    elif r.error == "payload":
        text += ", wrong content"
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
    if ep.kind == "heartbeat":
        return await probe_heartbeat(session, ep, sem)
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
                error, problem = "", ""
                if latency > ep.timeout_ms:
                    ok, error = False, "timeout"
                elif ok:  # reachable and fast: now check what it actually said
                    problem = check_payload(ep, body)
                    if problem:
                        ok, error = False, "payload"
                result = ProbeResult(ok, status, latency, error, utc_now(), reason=resp.reason or "",
                                     check_failed=problem, body_bytes=len(body))
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


# --------------------------------------------------------------------------- #
# Content checks ("200 OK, but broken")
# --------------------------------------------------------------------------- #
_MISSING = object()
_PATH_RE = re.compile(r"\[(\d+)\]|[^.\[\]]+")


def json_get(doc: Any, path: str) -> Any:
    """Follow 'data.items[0].id' (a leading '$' or '$.' is allowed). _MISSING if absent."""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    cur = doc
    for m in _PATH_RE.finditer(path):
        if m.group(1) is not None:
            i = int(m.group(1))
            if not isinstance(cur, list) or i >= len(cur):
                return _MISSING
            cur = cur[i]
        else:
            key = m.group(0)
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
                cur = cur[int(key)]
            else:
                return _MISSING
    return cur


def _short(v: Any, limit: int = 40) -> str:
    text = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else f'"{v}"'
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_rule(doc: Any, rule: dict) -> str:
    """'' when the rule holds, otherwise a short plain-language reason."""
    path, kind = str(rule["path"]), rule.get("rule", "not_empty")
    v = json_get(doc, path)
    if v is _MISSING:
        return f"{path} is missing from the response"
    if kind == "not_empty" and v in (None, "", [], {}):
        return f"{path} is empty"
    if kind == "equals" and v != rule["value"]:
        return f"{path} is {_short(v)}, expected {_short(rule['value'])}"
    if kind == "not_equals" and v == rule["value"]:
        return f"{path} is {_short(v)}"
    if kind in ("min", "max"):
        n = len(v) if isinstance(v, (list, dict, str)) else v
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return f"{path} is {_short(v)}, expected a number or a list"
        if kind == "min" and n < rule["value"]:
            return f"{path} is {n}, expected at least {rule['value']}"
        if kind == "max" and n > rule["value"]:
            return f"{path} is {n}, expected at most {rule['value']}"
    return ""


def check_payload(ep: Endpoint, body: bytes) -> str:
    """'' if the body passes every configured content check, else the first failure."""
    if ep.min_body_bytes and len(body) < ep.min_body_bytes:
        return f"response was {len(body):,} bytes, expected at least {ep.min_body_bytes:,}"
    if not (ep.body_contains or ep.body_not_contains or ep.error_keywords or ep.json_rules):
        return ""
    text = body.decode("utf-8", "replace")
    low = text.lower()
    for phrase in ep.body_contains:
        if phrase.lower() not in low:
            return f"expected text {_short(phrase)} is missing"
    banned = list(ep.body_not_contains) + (list(ERROR_KEYWORDS) if ep.error_keywords else [])
    for phrase in banned:
        if phrase.lower() in low:
            return f"response contains {_short(phrase)}"
    if ep.json_rules:
        try:
            doc = json.loads(text)
        except ValueError:
            return "response is not valid JSON"
        for rule in ep.json_rules:
            why = check_rule(doc, rule)
            if why:
                return why
    return ""


def apply_size_drift(ep: Endpoint, st: dict[str, Any], r: ProbeResult) -> None:
    """Fail a healthy-looking result whose body suddenly shrank (e.g. a product feed that
    now returns an empty list with a 200). Learns the usual size from healthy checks."""
    if not ep.size_drop_pct or not r.ok or r.blocked or ep.kind != "http":
        return
    base, n = st.get("size_base"), st.get("size_n", 0)
    if base and n >= SIZE_MIN_SAMPLES and r.body_bytes < base * (1 - ep.size_drop_pct / 100):
        r.ok, r.error = False, "payload"
        r.check_failed = f"response was {r.body_bytes:,} bytes; it is usually about {round(base):,}"
        return
    st["size_base"] = r.body_bytes if not base else base * 0.9 + r.body_bytes * 0.1
    st["size_n"] = n + 1


# --------------------------------------------------------------------------- #
# Heartbeats (no-code automations check in with us)
# --------------------------------------------------------------------------- #
def fmt_every(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return "day" if d == 1 else f"{d} days"
    if minutes % 60 == 0:
        h = minutes // 60
        return "hour" if h == 1 else f"{h} hours"
    return f"{minutes} min"


def heartbeat_grace_min(ep: Endpoint) -> int:
    """Allowed lateness: 10% of the interval, at least one 5-minute check."""
    return max(5, round(ep.expect_every_min * 0.1))


async def probe_heartbeat(session: aiohttp.ClientSession, ep: Endpoint,
                          sem: asyncio.Semaphore) -> ProbeResult:
    """Ask the cronus-forms Worker when this heartbeat last checked in.
    Trouble on OUR side (Worker down, token missing) is reported as 'health unknown',
    never as the client's outage."""
    headers = {"Authorization": f"Bearer {HEARTBEAT_READ_TOKEN}"} if HEARTBEAT_READ_TOKEN else {}
    async with sem:
        start = time.perf_counter()
        try:
            async with session.get(ep.target_url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                latency = int((time.perf_counter() - start) * 1000)
                if resp.status != 200:
                    return ProbeResult(True, resp.status, latency, "heartbeat status unavailable",
                                       utc_now(), blocked=True)
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return ProbeResult(True, None, int((time.perf_counter() - start) * 1000),
                               "heartbeat status unavailable", utc_now(), blocked=True)
    last_ms = data.get("last_ping") if isinstance(data, dict) else None
    if not last_ms:
        return ProbeResult(True, 200, latency, "awaiting first heartbeat", utc_now(), blocked=True)
    last = datetime.fromtimestamp(float(last_ms) / 1000, timezone.utc)
    age = datetime.now(timezone.utc) - last
    allowed = timedelta(minutes=ep.expect_every_min + heartbeat_grace_min(ep))
    if age <= allowed:
        return ProbeResult(True, 200, latency, "", utc_now())
    return ProbeResult(False, 200, latency, "heartbeat", utc_now(),
                       check_failed=f"last check-in {fmt_duration(age)} ago; "
                                    f"it normally runs every {fmt_every(ep.expect_every_min)}")


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
    if r.error == "heartbeat":
        return ("The scheduled automation hasn't checked in. Open its run history (Zapier, Make or "
                "the cron log): look for a paused scenario, an expired app connection, or a step "
                "that started failing.")
    if r.error == "payload":
        return ("The server answered, but the response failed a content check. Check the latest "
                "deploy, expired API keys or tokens for upstream services, and code paths that "
                "return empty data instead of an error.")
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
    kind: str                   # incident, reminder, resolved, certificate, weekly, test
    text: str                   # Telegram HTML
    undo: dict[str, Any] = field(default_factory=dict)  # state to restore if delivery fails

    @property
    def is_incident(self) -> bool:
        return self.kind in ("incident", "reminder")


def impact(r: ProbeResult) -> tuple[str, str]:
    """(emoji, word) for the card header: is it down, or up but unhealthy?"""
    s = r.status
    if r.error == "heartbeat":
        return "🔴", "MISSED CHECK-IN"
    if r.error == "payload":
        return "🟠", "BROKEN RESPONSE"
    if s is None or s >= 500 or s == 404:
        return "🔴", "DOWN"
    if r.error == "timeout":
        return "🟠", "SLOW"
    return "🟠", "DEGRADED"


def esc(text: str) -> str:
    """Escape for Telegram HTML (quotes don't need it, and leaving them keeps JSON readable)."""
    return html.escape(text, quote=False)


def target_line(ep: Endpoint) -> str:
    """What the card says is being watched. Heartbeats never show our status URL."""
    if ep.kind == "heartbeat":
        return f"<code>heartbeat · expected every {esc(fmt_every(ep.expect_every_min))}</code>"
    return f"<code>{esc(ep.method)} {esc(ep.target_url)}</code>"


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
    if r.check_failed:
        rows.append(("CHECK", r.check_failed))
    rows.append(("FAILURES", f"{fails} consecutive checks"))
    if ep.kind == "http":
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
        target_line(ep),
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
        target_line(ep),
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
TELEGRAM_HINTS = {
    401: "The bot token is wrong or was revoked: update the TELEGRAM_BOT_TOKEN secret.",
    400: "Usually a wrong telegram_chat_id in ENDPOINTS_JSON, or the bot isn't in that group.",
    403: "The bot was removed from the group or blocked: add it back to the group.",
}


def telegram_error_line(status: int, chat_id: str, body: str) -> str:
    """Log line for a failed Telegram send; no chat ids unless VERBOSE."""
    if VERBOSE:
        return f"Telegram {status} for {chat_id}: {body[:300]}"
    try:
        why = str(json.loads(body).get("description", ""))
    except (json.JSONDecodeError, AttributeError):
        why = ""
    # Telegram descriptions don't contain ids, but strip long numbers just in case.
    why = re.sub(r"-?\d{6,}", "<id>", why)[:200]
    return f"Telegram {status} for <chat hidden>: {why or '(no description)'}"


async def send_telegram(session: aiohttp.ClientSession, chat_id: str, alert: Alert) -> bool:
    if not chat_id:      # a Slack-only client
        return True
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
                # Never print the token. The raw body can carry a chat id (a group upgraded
                # to a supergroup returns migrate_to_chat_id), so only VERBOSE logs print it.
                print(f"[alert] {telegram_error_line(resp.status, chat_id, body)}", file=sys.stderr)
                if IN_ACTIONS:  # surface Telegram's reason on the run page (no chat id, no token)
                    try:
                        why = json.loads(body).get("description", "")
                    except (json.JSONDecodeError, AttributeError):
                        why = ""
                    hint = TELEGRAM_HINTS.get(resp.status, "")
                    print(f"::warning title=cronus-telegram::Telegram {resp.status}: {why[:150]}. {hint}")
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"[alert] Telegram send error ({type(exc).__name__}), attempt {attempt + 1}", file=sys.stderr)
            await asyncio.sleep(2)
    return False


def slack_text(alert: Alert) -> str:
    links = "  ".join(f"<{u}|{t}>" for t, u in link_buttons(alert.ep))
    return to_plain(alert.text) + (f"\n{links}" if links else "")


# Slack answers a failed webhook post with a short error code as the body. These ones mean
# the webhook will never work again until the client reconnects (or an admin steps in).
SLACK_PERMANENT = {"no_service", "no_active_hooks", "channel_not_found", "channel_is_archived",
                   "invalid_token", "no_team", "team_disabled", "team_not_found", "user_not_found",
                   "action_prohibited", "posting_to_general_channel_denied"}
SLACK_HINTS = {
    "no_service": "The webhook was removed (app uninstalled or access revoked): ask the client to reconnect at cronussignals.com/connect.",
    "channel_not_found": "The channel was deleted: ask the client to reconnect at cronussignals.com/connect.",
    "channel_is_archived": "The channel was archived: ask the client to unarchive it or reconnect another channel.",
    "action_prohibited": "A Slack admin restricted posting to that channel.",
    "invalid_payload": "Cronus sent a message Slack couldn't read: a bug in sentinel.py, not the client.",
}
_slack_reported: set[str] = set()   # one report per webhook per run


def slack_code(body: str) -> str:
    """Slack's error code from a webhook response body (never the URL, which is a credential)."""
    code = body.strip().splitlines()[0] if body.strip() else ""
    return re.sub(r"[^a-z0-9_]", "", code.lower())[:60] or "(no reason)"


async def report_dead_slack(session: aiohttp.ClientSession, webhook: str, reason: str) -> None:
    """Tell cronus-forms a webhook is dead, so the inbox gets one email (sent only the URL's hash)."""
    digest = hashlib.sha256(webhook.encode()).hexdigest()
    if digest in _slack_reported or not HEARTBEAT_READ_TOKEN or not SLACK_DEAD_URL:
        return
    _slack_reported.add(digest)
    try:
        async with session.post(SLACK_DEAD_URL, json={"hook_sha256": digest, "reason": reason},
                                headers={"Authorization": f"Bearer {HEARTBEAT_READ_TOKEN}",
                                         "User-Agent": USER_AGENT},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"[alert] could not report the dead Slack webhook ({resp.status})", file=sys.stderr)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"[alert] could not report the dead Slack webhook ({type(exc).__name__})", file=sys.stderr)


async def send_slack(session: aiohttp.ClientSession, webhook: str, alert: Alert) -> bool:
    if not webhook:
        return True
    if DRY_RUN:
        if VERBOSE:
            print(f"--- DRY RUN → Slack [{alert.kind.upper()}] ---\n{slack_text(alert)}\n")
        return True
    for attempt in range(3):
        try:
            async with session.post(webhook, json={"text": slack_text(alert)},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                if resp.status == 429 and attempt < 2:   # rate limited: one message per second per channel
                    try:
                        wait = float(resp.headers.get("Retry-After", "1"))
                    except ValueError:
                        wait = 1.0
                    await asyncio.sleep(min(max(wait, 1.0), 10))
                    continue
                code = slack_code(body)
                print(f"[alert] Slack {resp.status}: {code}", file=sys.stderr)
                if IN_ACTIONS:   # the code only, never the webhook URL
                    print(f"::warning title=cronus-slack::Slack {resp.status}: {code}. {SLACK_HINTS.get(code, '')}".rstrip())
                if code in SLACK_PERMANENT or resp.status in (404, 410):
                    await report_dead_slack(session, webhook, f"{resp.status} {code}")
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"[alert] Slack send error ({type(exc).__name__}), attempt {attempt + 1}", file=sys.stderr)
            await asyncio.sleep(2)
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
    """One sample card per alert destination (Telegram chat / Slack webhook).

    With TEST_CLIENT set, only that client's destinations get a card, so a new
    client can be shown a test without every existing client receiving one too.
    """
    if TEST_CLIENT:
        endpoints = [ep for ep in endpoints if ep.client_id.lower() == TEST_CLIENT.lower()]
        if not endpoints:
            # No client ids in the message: Actions logs on a public repo are public.
            msg = "no endpoint has that test_client id; check the spelling against ENDPOINTS_JSON"
            if IN_ACTIONS:
                print(f"::error title=cronus-test-alert::{msg}")
            else:
                print(f"[test] {msg}", file=sys.stderr)
            return 1
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
    line = (f"{ok}/{len(targets)} test alert(s) delivered"
            + (" (one client)" if TEST_CLIENT else "") + (" (dry run)" if DRY_RUN else ""))
    if IN_ACTIONS:
        print(f"::notice title=cronus-test-alert::{line}")
        if ok < len(targets):
            print(f"::error title=cronus-test-alert::{len(targets) - ok} test alert(s) failed")
    else:
        print(f"[test] {line}")
    return 0 if ok == len(targets) else 1


# --------------------------------------------------------------------------- #
# Certificate expiry warnings
# --------------------------------------------------------------------------- #
async def fetch_cert(host: str, port: int) -> dict[str, Any] | None:
    """The server's verified certificate (expiry + issuer), or None if it can't be read.
    An invalid or expired certificate fails verification here; the normal probe
    already alerts on that as a TLS failure, so it isn't double-reported."""
    ctx = ssl.create_default_context()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout=10)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return None
    try:
        cert = writer.get_extra_info("peercert") or {}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
    if not cert.get("notAfter"):
        return None
    issuer = {k: v for part in cert.get("issuer", ()) for k, v in part}
    name = issuer.get("organizationName", "")
    cn = issuer.get("commonName", "")
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    return {"expires": expires.strftime(TS_FORMAT),
            "issuer": f"{name} ({cn})" if name and cn and cn != name else (name or cn)}


def cert_threshold(days_left: float, already: list[int]) -> int | None:
    """The tightest warning threshold just crossed that hasn't been sent for this certificate."""
    due = [t for t in CERT_WARN_DAYS if days_left <= t and t not in already]
    return min(due) if due else None


def format_cert_warning(ep: Endpoint, host: str, cert: dict[str, Any], now: datetime) -> str:
    e = esc
    expires = parse_ts(cert["expires"]) or now
    days = max(0, (expires - now).total_seconds() / 86400)
    rows = [("HOST", host), ("EXPIRES", fmt_when(expires, ep.timezone, now)),
            ("IN", f"{int(days)} days" if days >= 1 else f"{fmt_duration(expires - now)}")]
    if cert.get("issuer"):
        rows.append(("ISSUER", cert["issuer"]))
    panel = "\n".join(f"{k:<9} {e(v)}" for k, v in rows)
    return "\n".join([
        f"🟡 <b>CERTIFICATE EXPIRES SOON</b> · <b>{e(ep.endpoint_name)}</b>",
        f"<code>{e(ep.target_url)}</code>",
        f"<pre>{panel}</pre>",
        "<b>Next step:</b> Renew the certificate, or check that auto-renewal (certbot, or your "
        "host's managed SSL) is running. Once it lapses, browsers and webhook senders refuse to connect.",
        footer(ep),
    ])


async def check_certs(endpoints: list[Endpoint], state: dict[str, Any],
                      now: datetime) -> list[Alert]:
    """Read each HTTPS host's certificate at most every CERT_CHECK_EVERY_H hours, and
    warn each endpoint once per threshold (e.g. 14 and 3 days) per certificate."""
    from urllib.parse import urlsplit
    targets: dict[tuple[str, int], list[Endpoint]] = {}
    for ep in endpoints:
        u = urlsplit(ep.target_url)
        if ep.kind == "http" and u.scheme == "https" and u.hostname:
            targets.setdefault((u.hostname, u.port or 443), []).append(ep)

    due = []
    for hp, eps in targets.items():
        last = parse_ts(state[eps[0].key].get("cert_checked"))
        if last is None or now - last >= timedelta(hours=CERT_CHECK_EVERY_H):
            due.append(hp)
    certs = await asyncio.gather(*(fetch_cert(h, p) for h, p in due))

    alerts = []
    for (host, port), cert in zip(due, certs):
        for ep in targets[(host, port)]:
            st = state[ep.key]
            st["cert_checked"] = now.strftime(TS_FORMAT)
            if not cert:
                continue
            if st.get("cert_expires") != cert["expires"]:  # new or renewed certificate
                st.update(cert_expires=cert["expires"], cert_warned=[])
            st["cert_issuer"] = cert["issuer"]
            expires = parse_ts(cert["expires"])
            if not expires:
                continue
            t = cert_threshold((expires - now).total_seconds() / 86400, st.get("cert_warned", []))
            if t is not None:
                prev = list(st.get("cert_warned", []))
                # Mark every looser threshold too, so a cert first seen at 2 days left
                # doesn't trigger a "14 days" warning on the next check.
                st["cert_warned"] = sorted(set(prev) | {x for x in CERT_WARN_DAYS if x >= t})
                alerts.append(Alert(ep, "certificate", format_cert_warning(ep, host, cert, now),
                                    undo={"cert_warned": prev}))  # restored if delivery fails
    return alerts


# --------------------------------------------------------------------------- #
# Weekly summary
# --------------------------------------------------------------------------- #
def new_week(now: datetime) -> dict[str, Any]:
    return {"since": now.strftime(TS_FORMAT), "checks": 0, "ok": 0, "lat_sum": 0, "lat_n": 0,
            "incidents": 0, "down_s": 0}


def record_week(st: dict[str, Any], last: ProbeResult, opened: bool,
                recovered_after_s: float | None, now: datetime) -> None:
    """Add one run's outcome to the endpoint's weekly counters."""
    if last.blocked:  # health unknown; leave it out of uptime
        return
    for bucket, fresh in (("week", new_week), ("month", new_month)):
        w = st.setdefault(bucket, fresh(now))
        w["checks"] += 1
        if last.ok:
            w["ok"] += 1
            w["lat_sum"] += last.latency_ms
            w["lat_n"] += 1
        if opened:
            w["incidents"] += 1
        if recovered_after_s:
            w["down_s"] += int(recovered_after_s)


def new_month(now: datetime) -> dict[str, Any]:
    return {**new_week(now), "log": []}


MONTH_LOG_MAX = 25


def month_log_open(st: dict[str, Any], r: ProbeResult, now: datetime) -> None:
    """Remember each incident for the monthly report: when, and what the check saw."""
    log = st.setdefault("month", new_month(now)).setdefault("log", [])
    if len(log) < MONTH_LOG_MAX:
        log.append({"at": st.get("down_since") or r.checked_at,
                    "what": r.check_failed or status_text(r)})


def month_log_close(st: dict[str, Any], recovered_after_s: float | None) -> None:
    log = st.get("month", {}).get("log") or []
    if log and "down_s" not in log[-1]:
        log[-1]["down_s"] = int(recovered_after_s or 0)


def weekly_due(meta: dict[str, Any], now: datetime) -> bool:
    """True on the first run at/after the configured weekday and hour, once per week."""
    if now.weekday() != WEEKLY_DAY or now.hour < WEEKLY_HOUR_UTC:
        return False
    return meta.get("weekly_sent_on") != now.date().isoformat()


def fmt_pct(ok: int, total: int) -> str:
    if not total:
        return "—"
    p = 100 * ok / total
    return "100%" if ok == total else f"{min(p, 99.99):.2f}%".replace(".00%", "%")


def format_weekly(eps: list[Endpoint], state: dict[str, Any], now: datetime) -> str:
    e = esc
    first = eps[0]
    starts = [parse_ts(state[ep.key].get("week", {}).get("since")) for ep in eps]
    start = min((s for s in starts if s), default=now)
    blocks, down_now = [], 0
    for ep in eps:
        st = state[ep.key]
        w = st.get("week") or new_week(now)
        down_s = w["down_s"]
        if st.get("incident_open"):
            down_now += 1
            since = parse_ts(st.get("down_since"))
            if since:
                down_s += int((now - since).total_seconds())
        avg = f"avg {round(w['lat_sum'] / w['lat_n'])} ms" if w["lat_n"] else "avg —"
        inc = f"{w['incidents']} incident" + ("" if w["incidents"] == 1 else "s")
        if down_s:
            inc += f", ~{fmt_duration(timedelta(seconds=down_s))} down"
        lines = [e(ep.endpoint_name),
                 f"  uptime {fmt_pct(w['ok'], w['checks'])} · {w['checks']:,} checks",
                 f"  {avg} · {inc}"]
        exp = parse_ts(st.get("cert_expires"))
        if exp:
            lines.append(f"  certificate valid {max(0, (exp - now).days)} more days")
        blocks.append("\n".join(lines))

    if down_now:
        status = f"⚠️ {down_now} endpoint{'s are' if down_now > 1 else ' is'} down right now."
    else:
        status = "All endpoints were healthy at the time of this report."
    period = f"{start:%b %d}".replace(" 0", " ") + " – " + f"{now:%b %d}".replace(" 0", " ")
    return "\n".join([
        f"📊 <b>WEEKLY REPORT</b> · <b>{e(first.client_id)}</b>",
        f"{period} · checked every 5 minutes",
        "<pre>" + "\n\n".join(blocks) + "</pre>",
        status,
        "<i>Uptime counts checks that passed. A single failed check that passed on the "
        "20-second re-check counts as up.</i>",
        footer(first),
    ])


def weekly_groups(endpoints: list[Endpoint]) -> dict[str, list[Endpoint]]:
    """Endpoints grouped by alert destination (one report each), keyed by a hash so the
    state file never stores chat ids or webhook URLs in the clear."""
    groups: dict[str, list[Endpoint]] = {}
    for ep in endpoints:
        if ep.weekly_summary:
            dest = hashlib.sha256(f"{ep.telegram_chat_id}|{ep.slack_webhook_url}".encode()).hexdigest()[:12]
            groups.setdefault(dest, []).append(ep)
    return groups


async def send_weekly(session: aiohttp.ClientSession, endpoints: list[Endpoint],
                      state: dict[str, Any], now: datetime, preview: bool) -> None:
    """Send due weekly reports. A preview (manual run) sends to everyone and keeps the counters;
    the scheduled send resets each client's counters only once their report is delivered,
    so a failed delivery is retried on the next run instead of being lost."""
    meta = state.setdefault(META_KEY, {})
    sent_on = meta.setdefault("weekly_sent_on", {})
    today = now.date().isoformat()
    groups = weekly_groups(endpoints)
    if not preview:
        if now.weekday() != WEEKLY_DAY or now.hour < WEEKLY_HOUR_UTC:
            return
        groups = {d: eps for d, eps in groups.items() if sent_on.get(d) != today}
    if not groups:
        return
    for eps in groups.values():  # an endpoint with no history yet still gets a line
        for ep in eps:
            state.setdefault(ep.key, {}).setdefault("week", new_week(now))
    items = list(groups.items())
    ok = await asyncio.gather(*(dispatch(session, Alert(eps[0], "weekly", format_weekly(eps, state, now)))
                                for _, eps in items))
    if not preview:
        for (dest, eps), delivered in zip(items, ok):
            if delivered:
                sent_on[dest] = today
                for ep in eps:
                    state[ep.key]["week"] = new_week(now)
        for dest in [d for d in sent_on if d not in weekly_groups(endpoints)]:
            del sent_on[dest]
    line = f"{sum(ok)}/{len(ok)} weekly report(s) delivered" + (" (preview)" if preview else "") \
        + (" (dry run)" if DRY_RUN else "")
    if IN_ACTIONS:
        print(f"::notice title=cronus-weekly::{line}")
        if sum(ok) < len(ok):
            print(f"::warning title=cronus-weekly::{len(ok) - sum(ok)} weekly report(s) failed; "
                  "retrying on the next run")
    else:
        print(f"[weekly] {line}")


# --------------------------------------------------------------------------- #
# Branded monthly report (white-label: carries the agency's name, not ours)
# --------------------------------------------------------------------------- #
HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def monthly_groups(endpoints: list[Endpoint]) -> dict[str, list[Endpoint]]:
    """One report per client, keyed by a hash so state never holds client names."""
    groups: dict[str, list[Endpoint]] = {}
    for ep in endpoints:
        if ep.monthly_report:
            key = hashlib.sha256(ep.client_id.encode()).hexdigest()[:12]
            groups.setdefault(key, []).append(ep)
    return groups


def report_brand(eps: list[Endpoint]) -> dict[str, Any]:
    pick = lambda attr: next((getattr(ep, attr) for ep in eps if getattr(ep, attr)), "")  # noqa: E731
    color = pick("report_color")
    return {
        "agency": pick("report_brand"),
        "client": pick("client_name") or eps[0].client_id,
        "color": color if HEX_RE.fullmatch(color or "") else "#1f2937",
        "logo": pick("report_logo_url"),
        "rph": float(pick("revenue_per_hour") or 0),
        "tz": pick("timezone"),
    }


def _local(dt: datetime, tz: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz)) if tz else dt
    except (ZoneInfoNotFoundError, ValueError):
        return dt


def _day(dt: datetime) -> str:
    return f"{dt:%b} {dt.day}"


def month_summary(eps: list[Endpoint], state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Numbers for the report and its caption, with open incidents counted up to now."""
    rows, log = [], []
    tot = {"checks": 0, "ok": 0, "incidents": 0, "down_s": 0}
    starts = []
    for ep in eps:
        st = state.get(ep.key, {})
        m = st.get("month") or new_month(now)
        s0 = parse_ts(m.get("since"))
        if s0:
            starts.append(s0)
        down_s = m["down_s"]
        ongoing = bool(st.get("incident_open"))
        if ongoing:
            since = parse_ts(st.get("down_since"))
            if since:
                down_s += int((now - since).total_seconds())
        for k in ("checks", "ok", "incidents"):
            tot[k] += m[k]
        tot["down_s"] += down_s
        exp = parse_ts(st.get("cert_expires"))
        rows.append({
            "name": ep.endpoint_name,
            "type": "Automation check-in" if ep.kind == "heartbeat" else "API / webhook",
            "uptime": fmt_pct(m["ok"], m["checks"]),
            "avg": f"{round(m['lat_sum'] / m['lat_n'])} ms" if m["lat_n"] and ep.kind == "http" else "—",
            "checks": m["checks"], "incidents": m["incidents"],
            "down": fmt_duration(timedelta(seconds=down_s)) if down_s else "none",
            "cert": f"{max(0, (exp - now).days)} days left" if exp else "—",
            "ongoing": ongoing,
        })
        for i, item in enumerate(m.get("log", [])):
            last = i == len(m["log"]) - 1
            dur = item.get("down_s")
            log.append({"at": parse_ts(item.get("at")), "name": ep.endpoint_name,
                        "what": item.get("what", ""),
                        "dur": fmt_duration(timedelta(seconds=dur)) if dur
                        else ("ongoing" if last and ongoing else "under 5 min")})
    log.sort(key=lambda x: x["at"] or now)
    start = min(starts, default=now)
    return {"rows": rows, "log": log, "tot": tot, "start": start}


def format_monthly_html(eps: list[Endpoint], state: dict[str, Any], now: datetime) -> str:
    """A self-contained page the agency can forward or print to PDF for its client."""
    e = esc
    b = report_brand(eps)
    m = month_summary(eps, state, now)
    tot = m["tot"]
    agency = b["agency"] or "Cronus Signals"
    start, end = _local(m["start"], b["tz"]), _local(now, b["tz"])
    period = f"{_day(start)} – {_day(end)}, {end.year}"
    n = len(eps)
    if tot["incidents"] == 0:
        headline = "Every monitored integration stayed healthy this period."
    else:
        k = tot["incidents"]
        headline = (f"{k} issue{'s were' if k > 1 else ' was'} detected and flagged to "
                    f"{e(agency)} within minutes of the first failed check.")
    lede = (f"{n} integration{'s' if n != 1 else ''} behind {e(b['client'])} "
            f"{'were' if n != 1 else 'was'} checked {tot['checks']:,} times, around the clock.")

    tiles = [("Uptime", fmt_pct(tot["ok"], tot["checks"])), ("Checks run", f"{tot['checks']:,}"),
             ("Issues detected", str(tot["incidents"])),
             ("Total downtime", fmt_duration(timedelta(seconds=tot["down_s"])) if tot["down_s"] else "none")]
    tile_html = "".join(f'<div class="tile"><div class="v">{e(v)}</div><div class="k">{e(k)}</div></div>'
                        for k, v in tiles)
    row_html = "".join(
        f"<tr><td><b>{e(r['name'])}</b><br><span class=muted>{e(r['type'])}</span></td>"
        f"<td>{e(r['uptime'])}</td><td>{e(r['avg'])}</td><td>{r['incidents']}</td>"
        f"<td>{e(r['down'])}{' <span class=live>ongoing</span>' if r['ongoing'] else ''}</td>"
        f"<td>{e(r['cert'])}</td></tr>" for r in m["rows"])
    if m["log"]:
        log_html = "<div class=\"scroll\"><table><tr><th>When</th><th>Integration</th><th>What we saw</th><th>Duration</th></tr>" + "".join(
            f"<tr><td>{e(_day(_local(x['at'], b['tz'])) + ', ' + _local(x['at'], b['tz']).strftime('%H:%M')) if x['at'] else '—'}</td>"
            f"<td>{e(x['name'])}</td><td>{e(x['what'])}</td><td>{e(x['dur'])}</td></tr>" for x in m["log"]) + "</table></div>"
    else:
        log_html = '<p class="muted">No incidents this period.</p>'
    money = ""
    if b["rph"] and tot["down_s"]:
        at_risk = b["rph"] * tot["down_s"] / 3600
        money = (f'<div class="note"><b>Estimated sales at risk during downtime: ${at_risk:,.0f}</b><br>'
                 f'<span class="muted">Downtime above × ${b["rph"]:,.0f} per hour (the store\'s average, as '
                 f'provided). An estimate, not a measured loss.</span></div>')
    logo = (f'<img src="{e(b["logo"])}" alt="{e(agency)}" class="logo">' if b["logo"]
            else f'<div class="agency">{e(agency)}</div>')
    c = b["color"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(b['client'])} · Stability report · {e(period)}</title>
<style>
:root{{--brand:{c};--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#ffffff;--soft:#f9fafb}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--soft);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.page{{max-width:820px;margin:24px auto;background:var(--bg);border:1px solid var(--line);border-radius:12px;
overflow:hidden}}.bar{{height:6px;background:var(--brand)}}.wrap{{padding:32px 36px}}
header{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}}
.logo{{max-height:40px;max-width:200px}}.agency{{font-weight:700;font-size:18px;color:var(--brand)}}
.meta{{text-align:right;color:var(--muted);font-size:13px}}h1{{font-size:26px;margin:28px 0 6px;line-height:1.25}}
h2{{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:32px 0 10px}}
.muted{{color:var(--muted);font-size:13px}}.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:22px}}
.tile{{border:1px solid var(--line);border-radius:10px;padding:14px}}.tile .v{{font-size:22px;font-weight:700;
font-variant-numeric:tabular-nums}}.tile .k{{color:var(--muted);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th{{text-align:left;font-size:12px;color:var(--muted);
font-weight:600;border-bottom:1px solid var(--line);padding:8px 6px}}td{{border-bottom:1px solid var(--line);
padding:10px 6px;vertical-align:top;font-variant-numeric:tabular-nums}}
.live{{background:#fef2f2;color:#b91c1c;border-radius:6px;padding:1px 6px;font-size:12px}}
.scroll{{overflow-x:auto;-webkit-overflow-scrolling:touch}}.scroll table{{min-width:560px}}
.note{{margin-top:20px;border-left:4px solid var(--brand);background:var(--soft);padding:12px 14px;border-radius:6px}}
footer{{margin-top:32px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
@media (max-width:640px){{.wrap{{padding:22px 16px}}.tiles{{grid-template-columns:repeat(2,1fr)}}
.meta{{text-align:left}}table{{font-size:13px}}}}
@media print{{body{{background:#fff}}.page{{margin:0;border:0;border-radius:0}}}}
</style></head><body><div class="page"><div class="bar"></div><div class="wrap">
<header>{logo}<div class="meta">Stability report<br><b>{e(b['client'])}</b><br>{e(period)}</div></header>
<h1>{headline}</h1><p class="muted">{lede}</p>
<div class="tiles">{tile_html}</div>{money}
<h2>What we monitor</h2>
<div class="scroll"><table><tr><th>Integration</th><th>Uptime</th><th>Avg response</th><th>Issues</th><th>Downtime</th><th>Certificate</th></tr>{row_html}</table></div>
<h2>Incident log</h2>{log_html}
<footer>Prepared by {e(agency)} for {e(b['client'].rstrip('.'))}. Each integration is checked every 5 minutes; a failed
check is re-checked 20 seconds later before an alert is raised, so brief blips are not counted as outages.
Downtime is measured between checks.</footer>
</div></div></body></html>"""


def monthly_caption(eps: list[Endpoint], state: dict[str, Any], now: datetime) -> str:
    b, m = report_brand(eps), month_summary(eps, state, now)
    t = m["tot"]
    down = fmt_duration(timedelta(seconds=t["down_s"])) if t["down_s"] else "no downtime"
    return (f"📄 <b>MONTHLY REPORT</b> · <b>{esc(b['client'])}</b>\n"
            f"Uptime {fmt_pct(t['ok'], t['checks'])} · {t['checks']:,} checks · "
            f"{t['incidents']} issue{'s' if t['incidents'] != 1 else ''} · {down}\n"
            "The attached page is ready to forward to the client, or print to PDF.")


async def send_telegram_document(session: aiohttp.ClientSession, chat_id: str, filename: str,
                                 data: bytes, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        print("[report] TELEGRAM_BOT_TOKEN not set — report not sent", file=sys.stderr)
        return False
    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN).replace("/sendMessage", "/sendDocument")
    for attempt in range(3):
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "HTML")
        form.add_field("document", data, filename=filename, content_type="text/html")
        try:
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return True
                if resp.status == 429:
                    await asyncio.sleep(3)
                    continue
                print(f"[report] Telegram {resp.status}: {(await resp.text())[:200]}", file=sys.stderr)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2)
    return False


def report_filename(eps: list[Endpoint], now: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (report_brand(eps)["client"]).lower()).strip("-") or "client"
    return f"{slug}-stability-report-{now:%Y-%m}.html"


async def deliver_monthly(session: aiohttp.ClientSession, eps: list[Endpoint],
                          state: dict[str, Any], now: datetime) -> bool:
    page = format_monthly_html(eps, state, now).encode("utf-8")
    name = report_filename(eps, now)
    caption = monthly_caption(eps, state, now)
    ep = eps[0]
    if DRY_RUN:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / name).write_bytes(page)
        print(f"--- DRY RUN → monthly report written to {REPORTS_DIR / name}"
              if VERBOSE else "--- DRY RUN → monthly report written (set SENTINEL_VERBOSE_LOGS=1 for the path)")
        return True
    ok = True
    if ep.telegram_chat_id:
        ok = await send_telegram_document(session, ep.telegram_chat_id, name, page, caption)
    if ep.slack_webhook_url:  # Slack incoming webhooks can't carry files: send the numbers
        where = ("The full page was sent to your Telegram group and" if ep.telegram_chat_id
                 else "Slack can't carry the file itself; ask Cronus for the full page, which")
        ok = await send_slack(session, ep.slack_webhook_url,
                              Alert(ep, "monthly", caption.replace("The attached page", where))) and ok
    return ok


async def send_monthly(session: aiohttp.ClientSession, endpoints: list[Endpoint],
                       state: dict[str, Any], now: datetime, preview: bool) -> None:
    """On the 1st of the month (from MONTHLY_HOUR_UTC) send each client's branded report and
    reset its monthly counters once delivered. A preview sends now and keeps the counters."""
    meta = state.setdefault(META_KEY, {})
    sent = meta.setdefault("monthly_sent", {})
    month = now.strftime("%Y-%m")
    groups = monthly_groups(endpoints)
    if not preview:
        if now.day != 1 or now.hour < MONTHLY_HOUR_UTC:
            return
        groups = {k: v for k, v in groups.items() if sent.get(k) != month}
    if not groups:
        return
    items = list(groups.items())
    ok = await asyncio.gather(*(deliver_monthly(session, eps, state, now) for _, eps in items))
    if not preview:
        for (key, eps), delivered in zip(items, ok):
            if delivered:
                sent[key] = month
                for ep in eps:
                    state.setdefault(ep.key, {})["month"] = new_month(now)
    line = f"{sum(ok)}/{len(ok)} monthly report(s) delivered" + (" (preview)" if preview else "") \
        + (" (dry run)" if DRY_RUN else "")
    print(f"::notice title=cronus-monthly::{line}" if IN_ACTIONS else f"[monthly] {line}")


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #
async def run() -> int:
    state = load_state()
    endpoints = load_endpoints(state)
    if not endpoints:
        print("[sentinel] no enabled endpoints — nothing to do")
        return 0

    if TEST_ALERT:
        return await send_test_alerts(endpoints)

    now = datetime.now(timezone.utc)
    if SEND_WEEKLY:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            await send_weekly(session, endpoints, state, now, preview=True)
        return 0
    if SEND_MONTHLY:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            await send_monthly(session, endpoints, state, now, preview=True)
        return 0

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
            opened_now, recovered_after = False, None
            for r in results[ep.key]:
                apply_size_drift(ep, st, r)
                if r.blocked:  # health unknown (bot challenge, heartbeat not readable): change nothing
                    continue
                if r.ok:
                    since, up = parse_ts(st["down_since"]), parse_ts(r.checked_at)
                    if st["incident_open"] and since and up:
                        recovered_after = (up - since).total_seconds()
                    if st["incident_open"]:
                        alerts.append(Alert(ep, "resolved", format_resolved(ep, r, dict(st))))
                        resolved.append(idx[ep.key])
                        month_log_close(st, recovered_after)
                    st.update(failure_count=0, incident_open=False, down_since=None,
                              last_alert_at_count=0, last_fail_status=None)
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
                        opened_now = True
                        month_log_open(st, r, now)
                    st["incident_open"] = True
                    st["last_alert_at_count"] = n

            last = results[ep.key][-1]
            st.update(last_status=last.status, last_latency_ms=last.latency_ms,
                      last_checked=last.checked_at)
            record_week(st, last, opened_now, recovered_after, now)
            mark = "BLKD" if last.blocked else ("OK  " if last.ok else "FAIL")
            print(f"[{mark}] {label(ep, idx):<40} status={last.status} "
                  f"latency={last.latency_ms}ms fails={st['failure_count']}")

        # Drop state for endpoints removed from config.
        live = {ep.key for ep in endpoints}
        for k in [k for k in state if k not in live and k != META_KEY]:
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

        # Certificate warnings and the weekly report are informational: they are sent
        # separately and kept out of the Jarvis incident counts below.
        cert_alerts = await check_certs(endpoints, state, now)
        if cert_alerts:
            cert_ok = list(await asyncio.gather(*(dispatch(session, a) for a in cert_alerts)))
            for a, ok in zip(cert_alerts, cert_ok):
                if not ok:  # let the next check try this warning again
                    state[a.ep.key].update(a.undo, cert_checked=None)
            line = f"{sum(cert_ok)}/{len(cert_ok)} certificate warning(s) delivered"
            print(f"::notice title=cronus-certs::{line}" if IN_ACTIONS else f"[certs] {line}")
        await send_weekly(session, endpoints, state, now, preview=False)
        await send_monthly(session, endpoints, state, now, preview=False)

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
                print(f"::warning title=cronus-blocked::{len(blocked)} endpoint(s) have unknown health: a "
                      "bot-protection challenge (allow the monitor through, see ONBOARDING.md), a heartbeat "
                      "with no check-in yet, or the heartbeat status API is unreachable.")
            if summary["alerts_failed"]:
                print(f"::warning title=cronus-alert-failed::{summary['alerts_failed']} alert(s) failed to deliver")
        else:
            print(f"[summary] {line}")

    save_state(state)
    return 0


def new_heartbeat() -> int:
    """Print a fresh heartbeat id, the URL the client's automation should call, the D1
    statement that registers it, and the endpoints.json entry to add."""
    import secrets
    hb = "hb_" + secrets.token_hex(12)
    now_ms = int(time.time() * 1000)
    entry = {"client_id": "<client>", "endpoint_name": "<automation name>", "kind": "heartbeat",
             "heartbeat_id": hb, "expect_every_min": 60, "telegram_chat_id": "<chat id>"}
    print(f"Heartbeat id:   {hb}")
    print(f"Ping URL:       {HEARTBEAT_PING_URL.format(id=hb)}")
    print("  Add an HTTP/Webhooks step at the END of the Zap or Make scenario that calls this URL")
    print("  (GET or POST, no body needed), so a check-in means the whole run succeeded.\n")
    print("Register it (Cloudflare → D1 → cronus-onboarding → Console):")
    print(f"  insert into heartbeats (id, label, created_at) values ('{hb}', '<client> / <automation>', {now_ms});\n")
    print("Add to ENDPOINTS_JSON (set expect_every_min to how often it normally runs):")
    print("  " + json.dumps(entry))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["new-heartbeat"]:
        sys.exit(new_heartbeat())
    sys.exit(asyncio.run(run()))
