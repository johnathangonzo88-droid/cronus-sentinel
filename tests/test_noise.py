"""
Offline tests for noise control: alert_after_min, recover_after_min (flapping) and notify = "summary".

    python tests/test_noise.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel as S  # noqa: E402

PORT = 8771
BASE = f"http://127.0.0.1:{PORT}"
up = {}   # path name -> healthy?


async def target(request):
    return web.Response(status=200 if up.get(request.match_info["name"]) else 503, text="x")


def ep(name: str, **kw) -> dict:
    return {"client_id": "acme", "endpoint_name": name, "target_url": f"{BASE}/t/{name}",
            "telegram_chat_id": "-1001", "failure_threshold": 1, **kw}


def run(tmp: Path, cfg: dict) -> str:
    env = {**os.environ, "DRY_RUN": "1", "CONFIRM_DELAY_S": "0", "SENTINEL_VERBOSE_LOGS": "1",
           "WEEKLY_DAY": "7", "ENDPOINTS_JSON": json.dumps(cfg), "STATE_FILE": str(tmp / "state.json"),
           "REPORTS_DIR": str(tmp / "reports")}
    out = subprocess.run([sys.executable, str(ROOT / "sentinel.py")], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return out.stdout


def entry(tmp: Path) -> dict:
    state = json.loads((tmp / "state.json").read_text())
    return next(v for k, v in state.items() if k != S.META_KEY)


def age(tmp: Path, field: str, minutes: int) -> None:
    """Pretend a state timestamp happened `minutes` earlier (instead of waiting)."""
    path = tmp / "state.json"
    state = json.loads(path.read_text())
    for k, v in state.items():
        if k != S.META_KEY and v.get(field):
            v[field] = (S.parse_ts(v[field]) - timedelta(minutes=minutes)).strftime(S.TS_FORMAT)
    path.write_text(json.dumps(state))


def cards(out: str, kind: str) -> int:
    return out.count(f"[{kind}] ---")


def unit_tests():
    good, problems = S.parse_endpoints([ep("a", notify="SUMMARY", alert_after_min=10, recover_after_min=5)])
    assert not problems and good[0].notify == "summary" and good[0].alert_after_min == 10
    for bad in ({"notify": "loud"}, {"alert_after_min": "5"}, {"recover_after_min": -1},
                {"alert_after_min": 5000}, {"recover_after_min": True}):
        _, problems = S.parse_endpoints([ep("b", **bad)])
        assert problems, bad
    assert S.seconds_between("2026-10-14 10:00:00 UTC", "2026-10-14 10:05:30 UTC") == 330
    assert S.seconds_between(None, "2026-10-14 10:00:00 UTC") is None
    print("validation ✓")


def scenarios():
    with tempfile.TemporaryDirectory() as d:   # default behaviour is unchanged
        tmp, cfg = Path(d), {"endpoints": [ep("plain")]}
        up["plain"] = False
        assert cards(run(tmp, cfg), "INCIDENT") == 1
        up["plain"] = True
        assert cards(run(tmp, cfg), "RESOLVED") == 1
        print("default: alert on first confirmed failure, recover on first success ✓")

    with tempfile.TemporaryDirectory() as d:   # alert_after_min holds the incident
        tmp, cfg = Path(d), {"endpoints": [ep("slowburn", alert_after_min=10)]}
        up["slowburn"] = False
        assert cards(run(tmp, cfg), "INCIDENT") == 0 and not entry(tmp)["incident_open"]
        age(tmp, "down_since", 6)
        assert cards(run(tmp, cfg), "INCIDENT") == 0
        age(tmp, "down_since", 5)
        out = run(tmp, cfg)
        assert cards(out, "INCIDENT") == 1 and entry(tmp)["incident_open"], out
        print("alert_after_min: silent until down 10 min, then one incident ✓")

    with tempfile.TemporaryDirectory() as d:   # a short blip under alert_after_min never alerts
        tmp, cfg = Path(d), {"endpoints": [ep("blip", alert_after_min=10)]}
        up["blip"] = False
        run(tmp, cfg)
        up["blip"] = True
        out = run(tmp, cfg)
        assert cards(out, "INCIDENT") == 0 and cards(out, "RESOLVED") == 0
        assert entry(tmp)["down_since"] is None
        print("alert_after_min: a blip that recovers first sends nothing ✓")

    with tempfile.TemporaryDirectory() as d:   # recover_after_min absorbs flapping
        tmp, cfg = Path(d), {"endpoints": [ep("flappy", recover_after_min=10)]}
        up["flappy"] = False
        assert cards(run(tmp, cfg), "INCIDENT") == 1
        for _ in range(3):                        # up, down, up, down, up, down
            up["flappy"] = True
            out = run(tmp, cfg)
            assert cards(out, "RESOLVED") == 0, out
            up["flappy"] = False
            out = run(tmp, cfg)
            assert cards(out, "INCIDENT") == 0, out
        st = entry(tmp)
        assert st["incident_open"] and st["flaps"] == 3 and st["up_since"] is None
        up["flappy"] = True
        assert cards(run(tmp, cfg), "RESOLVED") == 0       # healthy, not yet for 10 min
        age(tmp, "up_since", 11)
        out = run(tmp, cfg)
        assert cards(out, "RESOLVED") == 1, out
        assert "FLAPPED" in out and "3x" in out and "STABLE" in out
        st = entry(tmp)
        assert not st["incident_open"] and not st["flaps"] and st["up_since"] is None
        print("recover_after_min: 3 flaps → one incident, one recovery after 10 min stable ✓")

    with tempfile.TemporaryDirectory() as d:   # notify = summary: tracked, never sent
        tmp, cfg = Path(d), {"endpoints": [ep("logs", notify="summary")]}
        up["logs"] = False
        out = run(tmp, cfg)
        assert "DRY RUN" not in out and entry(tmp)["incident_open"], out
        assert '"opened":[0]' in out                    # still counted for the reports
        up["logs"] = True
        out = run(tmp, cfg)
        assert "DRY RUN" not in out and not entry(tmp)["incident_open"]
        assert '"resolved":[0]' in out
        print("notify=summary: incident tracked for reports, no cards sent ✓")


async def main():
    unit_tests()
    app = web.Application()
    app.add_routes([web.get("/t/{name}", target)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    try:
        await asyncio.get_running_loop().run_in_executor(None, scenarios)
    finally:
        await runner.cleanup()
    print("\nall noise-control tests passed")


if __name__ == "__main__":
    asyncio.run(main())
