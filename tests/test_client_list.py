"""
Offline tests: endpoints from the cronus-forms client list (ENDPOINTS_URL) merged with
ENDPOINTS_JSON, the saved-copy fallback, and Slack-only clients.

    python tests/test_client_list.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
PORT = 8772
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "readtok"
served: dict = {"status": 200, "body": None}


async def ok(_):
    return web.Response(text="fine")


async def client_list(req):
    if req.headers.get("authorization") != f"Bearer {TOKEN}":
        return web.json_response({"ok": False}, status=401)
    if served["status"] != 200:
        return web.Response(status=served["status"], text="down")
    return web.json_response(served["body"])


def start_server() -> None:
    loop = asyncio.new_event_loop()
    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/api/sentinel/endpoints", client_list)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    threading.Thread(target=loop.run_forever, daemon=True).start()


def ep(client: str, name: str, **extra) -> dict:
    return {"client_id": client, "endpoint_name": name, "target_url": f"{BASE}/ok",
            "telegram_chat_id": "-100123", **extra}


SECRET = {"endpoints": [ep("cronus-internal", "Site")]}


def run(state: Path, secret: dict | None = SECRET, verbose: bool = False, **env_extra: str) -> tuple[int, str, dict]:
    env = {k: v for k, v in os.environ.items()
           if k not in {"ENDPOINTS_JSON", "SENTINEL_VERBOSE_LOGS", "TEST_ALERT", "TEST_CLIENT", "GITHUB_ACTIONS"}}
    env.update({"DRY_RUN": "1", "CONFIRM_DELAY_S": "0", "STATE_FILE": str(state),
                "ENDPOINTS_URL": f"{BASE}/api/sentinel/endpoints", "HEARTBEAT_READ_TOKEN": TOKEN,
                "SENTINEL_VERBOSE_LOGS": "1" if verbose else "0", **env_extra})
    if secret is not None:
        env["ENDPOINTS_JSON"] = json.dumps(secret)
    t = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env, cwd=state.parent,
                       capture_output=True, text=True, timeout=60)
    out = t.stdout + t.stderr
    summary = {}
    for line in out.splitlines():
        if line.startswith("[summary] "):
            summary = json.loads(line[len("[summary] "):])
    return t.returncode, out, summary


def main() -> None:
    start_server()
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "state.json"

        # 1. Secret + client list are both monitored.
        served.update(status=200, body={"version": 1, "endpoints": [
            ep("bright--shop", "Orders"), ep("bright--shop", "Feed", expected_status=[200])]})
        code, out, s = run(state)
        assert code == 0, out
        assert s["n"] == 3, out
        assert "1 from ENDPOINTS_JSON + 2 from the client list" in out, out
        assert "bright--shop" not in out and "Orders" not in out, out     # public logs: indexes only
        saved = json.loads(state.read_text())["__meta__"]
        assert len(saved["remote_endpoints"]) == 2 and saved["remote_at"], saved
        print("merge: secret + client list monitored, names kept out of the log ✓")

        # 2. Worker down: the saved copy keeps monitoring going.
        served["status"] = 503
        code, out, s = run(state)
        assert code == 0 and s["n"] == 3, out
        assert "client list unreachable (HTTPError); using the copy saved" in out, out
        print("fallback: unreachable list uses the saved copy ✓")

        # 3. Wrong token is treated the same way, never as "no clients".
        served["status"] = 200
        code, out, s = run(state, HEARTBEAT_READ_TOKEN="wrong")
        assert code == 0 and s["n"] == 3, out
        print("fallback: a rejected token keeps the saved copy ✓")

        # 4. No saved copy yet: secret only, with a warning.
        fresh = Path(d) / "fresh" / "state.json"
        fresh.parent.mkdir()
        served["status"] = 503
        code, out, s = run(fresh)
        assert code == 0 and s["n"] == 1, out
        assert "no saved copy yet" in out, out
        served["status"] = 200
        print("first run with the list down: ENDPOINTS_JSON still monitored ✓")

        # 5. One bad row is skipped; everyone else is still checked.
        served["body"] = {"endpoints": [ep("bright--shop", "Orders"),
                                        {"client_id": "bad", "endpoint_name": "x", "target_url": "ftp://x"},
                                        ep("bright--shop", "No channel", telegram_chat_id="")]}
        code, out, s = run(state)
        assert code == 0 and s["n"] == 2, out
        assert "2 row(s) in the client list skipped as invalid" in out, out
        assert "endpoint #2: needs a telegram_chat_id or a slack_webhook_url" in out, out
        print("bad rows: skipped with a warning, run continues ✓")

        # 6. The same endpoint in both places: the secret's copy wins.
        served["body"] = {"endpoints": [ep("cronus-internal", "Site", target_url="http://127.0.0.1:1/never")]}
        code, out, s = run(state)
        assert code == 0 and s["n"] == 1 and s["failing"] == [], out
        assert "1 client-list endpoint(s) also in ENDPOINTS_JSON" in out, out
        print("clash: ENDPOINTS_JSON entry wins ✓")

        # 7. Slack-only client: accepted, test card goes to Slack, Telegram is skipped.
        served["body"] = {"endpoints": [ep("slacky--site", "API", telegram_chat_id="",
                                           slack_webhook_url="https://hooks.slack.com/services/T/B/x")]}
        code, out, _ = run(state, verbose=True, TEST_ALERT="1", TEST_CLIENT="slacky--site")
        assert code == 0, out
        assert "1/1 test alert(s) delivered (one client)" in out, out
        assert "DRY RUN → Slack [TEST]" in out and "DRY RUN → Telegram chat" not in out, out
        print("slack-only client: test alert goes to Slack only ✓")

        # 8. No secret at all: the example endpoints.json is never monitored for real.
        served["body"] = {"endpoints": [ep("bright--shop", "Orders")]}
        code, out, s = run(state, secret=None)
        assert code == 0 and s["n"] == 1, out
        assert "0 from ENDPOINTS_JSON + 1 from the client list" in out, out
        print("no secret: example file ignored when a client list is configured ✓")

        # 9. Without ENDPOINTS_URL nothing changes (old behaviour).
        code, out, s = run(Path(d) / "old.json", ENDPOINTS_URL="")
        assert code == 0 and s["n"] == 1 and "client list" not in out, out
        print("no ENDPOINTS_URL: ENDPOINTS_JSON only, as before ✓")

    print("\nall client-list tests passed")


if __name__ == "__main__":
    main()
