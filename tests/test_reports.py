"""
Offline tests for the weekly report and certificate-expiry warnings.

    python tests/test_reports.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 8767
flip = {"down": False}


async def ok(_):    return web.json_response({"ok": True})
async def flaky(_): return web.Response(status=503 if flip["down"] else 200)


def run(tmp: Path, cfg: dict, **extra: str) -> str:
    env = {**os.environ, "DRY_RUN": "1", "CONFIRM_DELAY_S": "0", "SENTINEL_VERBOSE_LOGS": "1",
           "WEEKLY_DAY": "7", "ENDPOINTS_JSON": json.dumps(cfg),
           "STATE_FILE": str(tmp / "state.json"), **extra}
    out = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


async def weekly_tests():
    app = web.Application()
    app.add_routes([web.get("/ok", ok), web.get("/flaky", flaky)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    base = f"http://127.0.0.1:{PORT}"
    cfg = {"endpoints": [
        {"client_id": "acme", "endpoint_name": "Checkout API", "target_url": f"{base}/ok",
         "telegram_chat_id": "1"},
        {"client_id": "acme", "endpoint_name": "Orders webhook", "target_url": f"{base}/flaky",
         "telegram_chat_id": "1"},
        {"client_id": "other", "endpoint_name": "Other API", "target_url": f"{base}/ok",
         "telegram_chat_id": "2"},
        {"client_id": "quiet", "endpoint_name": "Opted out", "target_url": f"{base}/ok",
         "telegram_chat_id": "3", "weekly_summary": False},
    ]}
    loop = asyncio.get_running_loop()
    tmp = Path(tempfile.mkdtemp())
    go = lambda **kw: loop.run_in_executor(None, lambda: run(tmp, cfg, **kw))

    # 3 healthy runs, a 2-run outage (opens an incident), then recovery.
    for _ in range(3):
        await go()
    flip["down"] = True
    await go(); await go()
    flip["down"] = False
    rec = await go()
    assert "[RESOLVED]" in rec

    preview = await go(SEND_WEEKLY="1")
    assert preview.count("[WEEKLY]") == 2, preview          # one per destination, opted-out skipped
    acme = preview.split("<b>acme</b>")[1].split("--- DRY RUN")[0]
    assert "Checkout API\n  uptime 100% · 6 checks" in acme, acme
    assert "Orders webhook\n  uptime 66.67% · 6 checks" in acme, acme
    assert "1 incident" in acme and "avg" in acme and "All endpoints were healthy" in acme
    assert "Opted out" not in preview
    print("weekly preview: one report per client, correct uptime/incidents, opt-out respected ✓")

    # Preview keeps the counters; the scheduled send delivers once, then resets.
    today = datetime.now(timezone.utc)
    sched = {"WEEKLY_DAY": str(today.weekday()), "WEEKLY_HOUR_UTC": "0"}
    s1 = await go(**sched)
    assert s1.count("[WEEKLY]") == 2 and "uptime 71.43% · 7 checks" in s1, s1
    s2 = await go(**sched)
    assert "[WEEKLY]" not in s2, "weekly report sent twice in one day"
    state = json.loads((tmp / "state.json").read_text())
    assert state["acme::Orders webhook"]["week"]["checks"] == 1, state  # reset, then 1 new run
    assert all(len(k) == 12 for k in state["__meta__"]["weekly_sent_on"])  # hashed destinations
    print("weekly schedule: sends once on the day, resets counters, no chat ids in state ✓")

    # A client that is down when the report goes out is called out.
    flip["down"] = True
    await go(); await go()
    down = await go(SEND_WEEKLY="1")
    assert "1 endpoint is down right now" in down, down
    print("weekly report flags endpoints that are down right now ✓")
    await runner.cleanup()


async def cert_tests():
    import sentinel as s
    now = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
    ep = s.Endpoint("acme", "Checkout API", "https://api.acme.test/v1/orders", "1",
                    timezone="America/Chicago")
    state = {ep.key: {}}
    expiry = {"value": now + timedelta(days=20)}

    async def fake_fetch(host, port):
        assert (host, port) == ("api.acme.test", 443)
        return {"expires": expiry["value"].strftime(s.TS_FORMAT), "issuer": "Let's Encrypt (R11)"}
    s.fetch_cert = fake_fetch

    a = await s.check_certs([ep], state, now)
    assert a == [], "warned at 20 days"
    a = await s.check_certs([ep], state, now + timedelta(hours=1))
    assert a == [], "re-read the certificate before CERT_CHECK_EVERY_H passed"

    t = now + timedelta(days=7)                       # 13 days left → 14-day warning
    a = await s.check_certs([ep], state, t)
    assert len(a) == 1 and a[0].kind == "certificate"
    card = a[0].text
    for want in ("CERTIFICATE EXPIRES SOON", "api.acme.test", "IN        13 days",
                 "Let's Encrypt (R11)", "CDT", "Renew the certificate"):
        assert want in card, f"missing {want!r}:\n{card}"
    assert await s.check_certs([ep], state, t + timedelta(hours=13)) == [], "14-day warning repeated"

    a = await s.check_certs([ep], state, now + timedelta(days=17, hours=2))   # <3 days left
    assert len(a) == 1 and "IN        2 days" in a[0].text
    print("certificate warnings: 14 days and 3 days, once each, with local time and issuer ✓")

    expiry["value"] = now + timedelta(days=110)       # renewed
    a = await s.check_certs([ep], state, now + timedelta(days=18))
    assert a == [] and state[ep.key]["cert_warned"] == []
    print("certificate renewal resets the warnings ✓")

    fresh = {ep.key: {}}                              # first seen at 1 day left
    expiry["value"] = now + timedelta(days=1)
    a = await s.check_certs([ep], fresh, now)
    assert len(a) == 1 and fresh[ep.key]["cert_warned"] == [3, 14]
    assert await s.check_certs([ep], fresh, now + timedelta(hours=13)) == []
    print("certificate first seen near expiry: one warning, no stale 14-day warning ✓")

    http_ep = s.Endpoint("acme", "Plain", "http://api.acme.test/", "1")
    assert await s.check_certs([http_ep], {http_ep.key: {}}, now) == []
    print("plain-HTTP endpoints skipped ✓")


if __name__ == "__main__":
    asyncio.run(weekly_tests())
    asyncio.run(cert_tests())
    print("\nALL REPORT TESTS PASSED")
