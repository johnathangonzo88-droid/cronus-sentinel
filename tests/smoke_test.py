"""
Offline smoke test: spins up a local aiohttp server with healthy, 500, 404 and
slow routes, then runs sentinel.py against it twice to prove the failure
counter, incident alert, suppression and recovery logic.

    python tests/smoke_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
flip = {"recovered": False}


async def ok(_):        return web.json_response({"ok": True})
async def err(_):       return web.Response(status=200 if flip["recovered"] else 503)
async def missing(_):   return web.Response(status=404)
async def slow(_):
    await asyncio.sleep(1.5)
    return web.Response(text="late")
async def edge(_):      # looks like Cloudflare failing to reach the origin
    return web.Response(status=200 if flip["recovered"] else 522,
                        headers={"cf-ray": "8c1f2a9b3d4e5f60-ORD", "server": "cloudflare"},
                        text="<html><title>Connection timed out</title></html>", content_type="text/html")
async def leaky(_):     # an error body that contains a secret, which must be redacted
    return web.json_response({"error": "db down", "api_key": "sk_live_SHOULDNOTSHOW"}, status=500,
                             headers={"x-request-id": "req-abc123"})


def run_sentinel(tmp: Path, cfg: dict) -> str:
    env = {**os.environ, "DRY_RUN": "1", "CONFIRM_DELAY_S": "0", "SENTINEL_VERBOSE_LOGS": "1",
           "ENDPOINTS_JSON": json.dumps(cfg), "STATE_FILE": str(tmp / "state.json")}
    out = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


async def main():
    app = web.Application()
    app.add_routes([web.get("/ok", ok), web.get("/err", err),
                    web.get("/missing", missing), web.get("/slow", slow),
                    web.get("/edge", edge), web.get("/leaky", leaky)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    base = f"http://127.0.0.1:{PORT}"
    cfg = {"endpoints": [
        {"client_id": "t", "endpoint_name": "healthy", "target_url": f"{base}/ok", "telegram_chat_id": "1"},
        {"client_id": "t", "endpoint_name": "server-error", "target_url": f"{base}/err", "telegram_chat_id": "1"},
        {"client_id": "t", "endpoint_name": "not-found", "target_url": f"{base}/missing", "telegram_chat_id": "1"},
        {"client_id": "t", "endpoint_name": "too-slow", "target_url": f"{base}/slow", "telegram_chat_id": "1", "timeout_ms": 1000},
        {"client_id": "t", "endpoint_name": "dead-port", "target_url": "http://127.0.0.1:9", "telegram_chat_id": "1"},
    ]}
    rich = {"endpoints": [
        {"client_id": "acme", "endpoint_name": "Checkout API", "target_url": f"{base}/edge",
         "telegram_chat_id": "1", "severity": "critical", "timezone": "America/Chicago",
         "runbook_url": "https://example.com/runbook", "slack_webhook_url": "https://hooks.example/x"},
        {"client_id": "acme", "endpoint_name": "Orders API", "target_url": f"{base}/leaky",
         "telegram_chat_id": "1"},
    ]}

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        loop = asyncio.get_running_loop()

        r1 = await loop.run_in_executor(None, run_sentinel, tmp, cfg)
        assert "INCIDENT" not in r1, "alerted after only 1 failure"
        print("run 1: 1st failure counted, no alert ✓")

        r2 = await loop.run_in_executor(None, run_sentinel, tmp, cfg)
        assert r2.count("INCIDENT") == 4, r2
        for name in ("server-error", "not-found", "too-slow", "dead-port"):
            assert name in r2
        assert "healthy" not in r2.split("DRY RUN", 1)[1]
        print("run 2: 4 incidents fired on 2nd consecutive failure ✓")

        r3 = await loop.run_in_executor(None, run_sentinel, tmp, cfg)
        assert "INCIDENT" not in r3
        print("run 3: open incidents suppressed (no spam) ✓")

        flip["recovered"] = True
        r4 = await loop.run_in_executor(None, run_sentinel, tmp, cfg)
        assert r4.count("RESOLVED") == 1 and "server-error" in r4
        print("run 4: recovery alert sent ✓")

        # Default (non-verbose) logs, as on public GitHub: no names, URLs or chat ids.
        flip["recovered"] = False
        quiet_tmp = Path(tempfile.mkdtemp())
        env = {**os.environ, "DRY_RUN": "1", "CONFIRM_DELAY_S": "0", "GITHUB_ACTIONS": "true",
               "ENDPOINTS_JSON": json.dumps(cfg), "STATE_FILE": str(quiet_tmp / "s.json")}
        env.pop("SENTINEL_VERBOSE_LOGS", None)
        quiet = lambda: subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                                       capture_output=True, text=True, timeout=60).stdout
        outs = [await loop.run_in_executor(None, quiet) for _ in range(2)]
        q = "\n".join(outs)
        for secret in ("server-error", "127.0.0.1", "healthy", "too-slow"):
            assert secret not in q, f"leaked {secret!r} into the public log:\n{q}"
        notice = [l for l in outs[1].splitlines() if l.startswith("::notice title=cronus-summary::")]
        assert len(notice) == 1, outs[1]
        summary = json.loads(notice[0].split("::", 2)[2])
        keys = [f"{e['client_id']}::{e['endpoint_name']}" for e in cfg["endpoints"]]
        import hashlib
        assert summary["cfg"] == hashlib.sha256("\n".join(keys).encode()).hexdigest()[:12]
        assert sorted(summary["opened"]) == [1, 2, 3, 4], summary
        assert summary["alerts_sent"] == 4 and summary["alerts_failed"] == 0
        print("run 5: public log masked; cronus-summary annotation correct ✓")

        # Rich incident cards: evidence, diagnosis, local time, buttons, redaction.
        flip["recovered"] = False
        rich_tmp = Path(tempfile.mkdtemp())
        healthy_first = {"endpoints": [dict(rich["endpoints"][0], target_url=f"{base}/ok")]}
        await loop.run_in_executor(None, run_sentinel, rich_tmp, healthy_first)  # records LAST OK
        await loop.run_in_executor(None, run_sentinel, rich_tmp, rich)
        c = await loop.run_in_executor(None, run_sentinel, rich_tmp, rich)
        edge_card = c.split("[INCIDENT]")[1]
        for want in ("🔴 <b>DOWN</b> · <b>Checkout API</b> · CRITICAL", "GET http://127.0.0.1",
                     "522 ", "SINCE", "LAST OK", "UTC · ", "SERVED BY Cloudflare",
                     "REQ ID    cf-ray 8c1f2a9b3d4e5f60-ORD", "page title: Connection timed out",
                     "Between Cloudflare and your server", "[button] Runbook → https://example.com/runbook",
                     "<i>Cronus Sentinel · acme</i>"):
            assert want in edge_card, f"missing {want!r} in:\n{edge_card}"
        assert "none since monitoring began" not in edge_card
        slack = c.split("DRY RUN → Slack [INCIDENT]")[1].split("--- DRY RUN")[0]
        assert "*DOWN*" in slack and "<https://example.com/runbook|Runbook>" in slack, slack
        assert "<title>" not in slack and "<b>" not in slack
        leaky_card = c.split("<b>Orders API</b>")[1].split("--- DRY RUN")[0]
        assert "x-request-id req-abc123" in leaky_card and "500 Internal Server Error" in leaky_card
        assert "SHOULDNOTSHOW" not in c and "[redacted]" in leaky_card, leaky_card
        print("run 6: incident card carries evidence, diagnosis, local time, buttons; secrets redacted ✓")

        flip["recovered"] = True
        rcv = await loop.run_in_executor(None, run_sentinel, rich_tmp, rich)
        card = rcv.split("[RESOLVED]")[1]
        for want in ("🟢 <b>RECOVERED</b> · <b>Checkout API</b>", "DOWNTIME  ~", "FAILED    2 checks",
                     "CAUSE     522"):
            assert want in card, f"missing {want!r} in:\n{card}"
        print("run 7: recovery card shows downtime, failed checks and cause ✓")

        env = {**os.environ, "DRY_RUN": "1", "TEST_ALERT": "1", "SENTINEL_VERBOSE_LOGS": "1",
               "ENDPOINTS_JSON": json.dumps(rich), "STATE_FILE": str(rich_tmp / "t.json")}
        t = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                           capture_output=True, text=True, timeout=60)
        assert t.returncode == 0 and "TEST ALERT" in t.stdout and "sample" in t.stdout, t.stdout + t.stderr
        assert "[test] 2/2 test alert(s) delivered" in t.stdout
        print("run 8: test alert shows a fully filled sample card ✓")
        print("\n--- sample rich incident card ---")
        print(edge_card.split("--- DRY RUN")[0])
        print("\n--- sample incident payload ---")
        print(r2.split("--- DRY RUN", 2)[1][:900])

    await runner.cleanup()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
