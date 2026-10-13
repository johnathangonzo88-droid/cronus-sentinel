"""
Offline tests: nothing that identifies a client reaches the (public) Actions log.

    python tests/test_log_privacy.py
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
sys.path.insert(0, str(ROOT))

PORT = 8771
CHAT = "-1009876543210"
NEW_CHAT = "-1001112223334"
SECRET_CLIENT = "acme-agency--bobs-bikes"


def run(env_extra: dict, cfg: dict, tmp: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in {"TEST_CLIENT", "SENTINEL_VERBOSE_LOGS", "GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME"}}
    env.update({"DRY_RUN": "1", "TEST_ALERT": "1", "ENDPOINTS_JSON": json.dumps(cfg),
                "STATE_FILE": str(tmp / "s.json"), **env_extra})
    return subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                          capture_output=True, text=True, timeout=60)


def test_test_client_from_event_payload() -> None:
    cfg = {"endpoints": [
        {"client_id": SECRET_CLIENT, "endpoint_name": "Orders", "target_url": "https://example.com",
         "telegram_chat_id": "111"},
        {"client_id": "other", "endpoint_name": "API", "target_url": "https://example.com",
         "telegram_chat_id": "222"}]}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        event = tmp / "event.json"
        event.write_text(json.dumps({"inputs": {"test_client": SECRET_CLIENT, "test_alert": True}}))
        gh = {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "workflow_dispatch"}

        t = run(gh, cfg, tmp)
        out = t.stdout + t.stderr
        assert t.returncode == 0, out
        assert "1/1 test alert(s) delivered (one client)" in out, out
        assert SECRET_CLIENT not in out, out

        # Other events never read dispatch inputs (a scheduled run has none).
        t = run({**gh, "GITHUB_EVENT_NAME": "schedule"}, cfg, tmp)
        assert "2/2 test alert(s) delivered" in t.stdout + t.stderr, t.stdout + t.stderr

        # The env var still wins for local runs.
        t = run({**gh, "TEST_CLIENT": "other"}, cfg, tmp)
        assert "1/1 test alert(s) delivered (one client)" in t.stdout + t.stderr

        # An unknown id from the payload fails without echoing it.
        event.write_text(json.dumps({"inputs": {"test_client": "nobody-xyz"}}))
        t = run(gh, cfg, tmp)
        assert t.returncode == 1 and "nobody-xyz" not in t.stdout + t.stderr
    print("event payload: test_client applied, never printed ✓")


def test_workflow_does_not_pass_test_client_in_env() -> None:
    wf = (ROOT / ".github/workflows/sentinel.yml").read_text()
    assert "inputs.test_client" not in wf, "test_client must not go through env: (it is logged)"
    print("workflow: test_client not in the step env ✓")


async def telegram_migrated(_req):
    return web.json_response({"ok": False, "error_code": 400,
                              "description": "Bad Request: group chat was upgraded to a supergroup chat",
                              "parameters": {"migrate_to_chat_id": int(NEW_CHAT)}}, status=400)


async def check_telegram_error_redacted(verbose: bool) -> str:
    os.environ["SENTINEL_VERBOSE_LOGS"] = "1" if verbose else "0"
    for k in ("DRY_RUN", "TEST_CLIENT"):
        os.environ.pop(k, None)
    sys.modules.pop("sentinel", None)
    import sentinel as S
    S.TELEGRAM_BOT_TOKEN = "x"
    S.TELEGRAM_API = f"http://127.0.0.1:{PORT}/bot{{token}}/sendMessage"

    app = web.Application()
    app.router.add_post("/botx/sendMessage", telegram_migrated)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    try:
        ep = S.Endpoint(client_id="c", endpoint_name="e", target_url="https://example.com",
                        telegram_chat_id=CHAT)
        import io
        import contextlib
        buf = io.StringIO()
        async with S.aiohttp.ClientSession() as session:
            with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                ok = await S.send_telegram(session, CHAT, S.Alert(ep, "test", "hi"))
        assert ok is False
        return buf.getvalue()
    finally:
        await runner.cleanup()


def test_telegram_error_body_redacted() -> None:
    out = asyncio.run(check_telegram_error_redacted(verbose=False))
    assert "upgraded to a supergroup" in out, out
    assert NEW_CHAT not in out and NEW_CHAT.lstrip("-") not in out and CHAT not in out, out
    out = asyncio.run(check_telegram_error_redacted(verbose=True))
    assert NEW_CHAT.lstrip("-") in out, out          # verbose (local debugging) keeps the detail
    print("telegram errors: no chat ids unless verbose ✓")


if __name__ == "__main__":
    test_test_client_from_event_payload()
    test_workflow_does_not_pass_test_client_in_env()
    test_telegram_error_body_redacted()
    print("\nall log-privacy tests passed")
