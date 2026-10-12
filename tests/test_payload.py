"""
Offline tests for content checks, size drift, heartbeats and the branded monthly report.

    python tests/test_payload.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from functools import partial
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel as S  # noqa: E402

PORT = 8768
BASE = f"http://127.0.0.1:{PORT}"
feed = {"items": [{"id": i, "title": f"Product {i}"} for i in range(40)]}
hb_last = {"ms": None}


async def good(_):       return web.json_response({"status": "ok", "data": {"orders": [1, 2, 3]}})
async def empty(_):      return web.json_response({"status": "ok", "data": {"orders": []}})
async def oops(_):       return web.Response(text="<h1>Something went wrong</h1> Fatal error in checkout.php")
async def html_ok(_):    return web.Response(text="<form id=lead-form>Get a quote</form>")
async def products(_):   return web.json_response(feed)
async def hb_status(r):
    if r.headers.get("authorization") != "Bearer tok":
        return web.json_response({"ok": False}, status=401)
    return web.json_response({"ok": True, "last_ping": hb_last["ms"]})


def run(tmp: Path, cfg: dict, **extra: str) -> str:
    env = {**os.environ, "DRY_RUN": "1", "CONFIRM_DELAY_S": "0", "SENTINEL_VERBOSE_LOGS": "1",
           "WEEKLY_DAY": "7", "ENDPOINTS_JSON": json.dumps(cfg), "STATE_FILE": str(tmp / "state.json"),
           "REPORTS_DIR": str(tmp / "reports"), "HEARTBEAT_READ_TOKEN": "tok",
           "HEARTBEAT_STATUS_URL": BASE + "/hb/{id}/status", "SIZE_MIN_SAMPLES": "3", **extra}
    out = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return out.stdout


def ep(name: str, path: str, **kw) -> dict:
    return {"client_id": "acme", "endpoint_name": name, "target_url": BASE + path,
            "telegram_chat_id": "-1001", "failure_threshold": 1, **kw}


def unit_tests():
    doc = {"data": {"items": [{"id": 7}], "count": 0, "name": ""}}
    assert S.json_get(doc, "$.data.items[0].id") == 7
    assert S.json_get(doc, "data.items.0.id") == 7
    assert S.json_get(doc, "data.missing") is S._MISSING
    assert S.check_rule(doc, {"path": "data.items"}) == ""
    assert S.check_rule(doc, {"path": "data.name"}) == "data.name is empty"
    assert S.check_rule(doc, {"path": "data.count", "rule": "min", "value": 1}) == \
        "data.count is 0, expected at least 1"
    assert S.check_rule(doc, {"path": "data.items", "rule": "min", "value": 2}) == \
        "data.items is 1, expected at least 2"
    assert "missing" in S.check_rule(doc, {"path": "data.nope", "rule": "exists"})
    assert S.validate_rules([{"path": "a", "rule": "min"}]).endswith('needs a "value"')
    assert S.validate_rules([{"rule": "exists"}]).endswith('needs a "path"')
    assert S.validate_rules([{"path": "a", "rule": "nope"}]).startswith("json_rules[0]: rule must")
    assert S.fmt_every(60) == "hour" and S.fmt_every(1440) == "day" and S.fmt_every(15) == "15 min"
    print("json paths, rules and validation ✓")


async def main():
    unit_tests()
    app = web.Application()
    app.add_routes([web.get("/good", good), web.get("/empty", empty), web.get("/oops", oops),
                    web.get("/form", html_ok), web.get("/products", products),
                    web.get("/hb/{id}/status", hb_status)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    loop = asyncio.get_running_loop()
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = {"endpoints": [
                ep("Orders API", "/good", json_rules=[{"path": "data.orders"}]),
                ep("Orders API (empty)", "/empty", json_rules=[{"path": "data.orders", "rule": "not_empty"}]),
                ep("Checkout", "/oops", error_keywords=True),
                ep("Lead form", "/form", body_contains=["lead-form"]),
                ep("Lead form (renamed)", "/form", body_contains=["contact-form"]),
            ]}
            out = await loop.run_in_executor(None, run, tmp, cfg)
            assert "[OK  ] acme::Orders API " in out, out
            assert "[OK  ] acme::Lead form " in out, out
            assert "BROKEN RESPONSE</b> · <b>Orders API (empty)" in out, out
            assert "data.orders is empty" in out
            assert 'response contains "fatal error"' in out or 'response contains "something went wrong"' in out
            assert 'expected text "contact-form" is missing' in out
            assert "200 OK, wrong content" in out
            assert "content check" in out  # the next-step advice
            print("200 OK but broken: JSON rules, error phrases and required text all alert ✓")

            # Bad config is rejected up front, with the reason.
            env = {**os.environ, "ENDPOINTS_JSON": json.dumps({"endpoints": [
                ep("x", "/good", json_rules=[{"path": "a", "rule": "min"}])]}),
                "STATE_FILE": str(tmp / "s2.json")}
            bad = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                                 capture_output=True, text=True, timeout=30)
            assert bad.returncode == 2 and 'needs a "value"' in bad.stderr
            print("malformed json_rules rejected with a clear message ✓")

        # Size drift: learn the usual size, then alert when the feed suddenly shrinks.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = {"endpoints": [ep("Product feed", "/products", size_drop_pct=60, failure_threshold=1)]}
            for _ in range(4):
                assert "[OK  ]" in await loop.run_in_executor(None, run, tmp, cfg)
            feed["items"] = feed["items"][:2]
            out = await loop.run_in_executor(None, run, tmp, cfg)
            assert "BROKEN RESPONSE" in out and "it is usually about" in out, out
            print("size drift: a feed that shrinks by 60%+ raises an alert ✓")

        # Heartbeats
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hb = {"client_id": "acme", "endpoint_name": "Orders → CRM zap", "kind": "heartbeat",
                  "heartbeat_id": "hb_" + "ab" * 12, "expect_every_min": 60,
                  "telegram_chat_id": "-1001", "failure_threshold": 1}
            cfg = {"endpoints": [hb]}
            out = await loop.run_in_executor(None, run, tmp, cfg)
            assert "[BLKD]" in out and "DRY RUN" not in out, out          # no ping yet: unknown, no alert
            hb_last["ms"] = int((time.time() - 20 * 60) * 1000)
            out = await loop.run_in_executor(None, run, tmp, cfg)
            assert "[OK  ]" in out, out
            hb_last["ms"] = int((time.time() - 3 * 3600) * 1000)
            out = await loop.run_in_executor(None, run, tmp, cfg)
            assert "MISSED CHECK-IN</b> · <b>Orders → CRM zap" in out, out
            assert "heartbeat · expected every hour" in out
            assert "it normally runs every hour" in out
            assert "/hb/" not in out.split("MISSED CHECK-IN")[1]          # never shows our status URL
            assert "run history" in out
            # Our status API failing must not resolve or open anything.
            out = await loop.run_in_executor(None, partial(run, tmp, cfg, HEARTBEAT_READ_TOKEN="wrong"))
            assert "[BLKD]" in out and "RECOVERED" not in out, out
            hb_last["ms"] = int(time.time() * 1000)
            out = await loop.run_in_executor(None, run, tmp, cfg)
            assert "RECOVERED</b> · <b>Orders → CRM zap" in out, out
            print("heartbeats: silence alerts, check-in recovers, our own outages stay quiet ✓")

            # Branded monthly report from the same state (one incident, now resolved).
            cfg["endpoints"][0].update(client_name="Acme Outdoor Co.", report_brand="Northwind Studio",
                                       report_color="#0e7490", revenue_per_hour=500,
                                       timezone="America/Chicago")
            cfg["endpoints"].append(ep("Orders API", "/good"))
            # Back-to-back test runs make the outage last seconds; give it a realistic 2 hours.
            state = json.loads((tmp / "state.json").read_text())
            state["acme::Orders → CRM zap"]["month"]["down_s"] = 7200
            state["acme::Orders → CRM zap"]["month"]["log"][0]["down_s"] = 7200
            (tmp / "state.json").write_text(json.dumps(state))
            out = await loop.run_in_executor(None, partial(run, tmp, cfg, SEND_MONTHLY="1"))
            assert "monthly report written" in out, out
            page = next((tmp / "reports").glob("acme-outdoor-co-stability-report-*.html")).read_text()
            for want in ("Northwind Studio", "Acme Outdoor Co.", "--brand:#0e7490",
                         "1 issue was detected and flagged to Northwind Studio",
                         "Orders → CRM zap", "Automation check-in", "API / webhook",
                         "Incident log", "Estimated sales at risk during downtime: $1,000", "2 h 00 min", "An estimate, not a measured loss"):
                assert want in page, want
            assert "Cronus" not in page  # white-label
            print("monthly report: branded, white-label, incident log and sales-at-risk estimate ✓")
    finally:
        await runner.cleanup()
    print("\nALL PAYLOAD/HEARTBEAT/REPORT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
