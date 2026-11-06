#!/usr/bin/env python3
"""Owner alarm for the silent failures /healthz cannot fix by restarting: Apple refusing every request (blocked IP)
or the poller not completing passes. Run it every 10 min (cron / launchd); two bad
checks in a row email the owner once (then once an hour while it lasts). State: data/watchdog.json."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

SITE = os.environ.get("SITE", "https://duo.solescout.ai")
OWNER = os.environ.get("OWNER_EMAIL", "")      # where the alarm goes
STATE = Path(__file__).resolve().parents[1] / "data" / "watchdog.json"
REPEAT_S = 3600


def problem() -> str | None:
    try:
        h = httpx.get(f"{SITE}/healthz", timeout=20).json()
    except Exception as e:  # noqa: BLE001
        return f"site unreachable: {type(e).__name__}"
    age, apple = h.get("last_cycle_age"), h.get("apple") or {}
    if age is None or age > 300:
        return f"poller stalled: last pass {age}s ago"
    if apple.get("requests", 0) == 0 and apple.get("errors", 0) > 0:
        return f"Apple refused every stock check in the last pass ({apple['errors']} errors) — blocked IP or proxies down"
    return None



def main() -> None:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    issue = problem()
    if not issue:
        if state.get("bad"):
            print("recovered")
        STATE.write_text("{}")
        return
    state["bad"] = state.get("bad", 0) + 1
    if state["bad"] >= 2 and time.time() - state.get("mailed_at", 0) > REPEAT_S:
        key = os.environ["RESEND_API_KEY"]
        httpx.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {key}"}, timeout=20, json={
            "from": "GetDuoNow watchdog <alerts@duo.solescout.ai>", "to": [OWNER],
            "subject": "GetDuoNow: stock checks are failing",
            "text": f"{issue}\n\nChecked {SITE}/healthz twice, 10 minutes apart. Alerts to customers are not going out "
                    "until this is fixed. Usual fix: refresh the PROXIES exits on Render (scripts in the GetDuoNow repo)."})
        state["mailed_at"] = time.time()
        print("emailed:", issue)
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
