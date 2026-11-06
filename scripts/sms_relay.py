#!/usr/bin/env python3
"""Interim SMS relay (operator's Mac): pull queued texts from the deployed site and hand them to a local sender.

Pulls `GET <SITE>/api/sms-queue` every 5 s (header X-Token) and appends each text as a JSONL job
`{"text", "to": [[phone, "SMS"]]}` to OUTBOX, which a separate process with macOS Messages permission sends.
Inbound: in the same loop, forwards customers' replies (STOP / HELP / START …) from ~/Library/Messages/chat.db to
`POST <SITE>/api/sms-inbound` (needs Full Disk Access). Last forwarded ROWID lives next to OUTBOX; the first run
starts at the newest message, so history is never replayed.
Retire this once Twilio is live.

    SITE=https://getduonow.onrender.com SMS_PULL_TOKEN=… OUTBOX=/path/outbox.jsonl python3 scripts/sms_relay.py
"""
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime

import httpx

SITE, TOKEN, OUTBOX = os.environ["SITE"].rstrip("/"), os.environ["SMS_PULL_TOKEN"], os.environ["OUTBOX"]
# iMessage until the iPhone's Text Message Forwarding to this Mac works again (11-01: SMS via the Mac → error 4)
SERVICE = os.environ.get("RELAY_SERVICE", "iMessage")
CHAT_DB = os.path.expanduser(os.environ.get("CHAT_DB", "~/Library/Messages/chat.db"))
STATE = os.path.join(os.path.dirname(os.path.abspath(OUTBOX)), "sms_relay_inbound.rowid")
# pre-launch the site sits behind ACCESS_KEY; the share-link cookie gets the relay through
INBOUND_HEADERS = {"X-Token": TOKEN, **({"Cookie": f"gdn_key={os.environ['ACCESS_KEY']}"} if os.environ.get("ACCESS_KEY") else {})}
PHONE = re.compile(r"\+?\d{10,15}")
MAX_LEN = 20   # commands are one word: longer texts are the operator's own conversations and never leave this Mac


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Newer macOS leaves message.text NULL and keeps the text in attributedBody (an NSArchiver typedstream):
    ... b"NSString" <class info> b"+" <length> <utf-8 bytes>; length is 1 byte, or 0x81 + int16 / 0x82 + int32 (LE)."""
    if not blob or b"NSString" not in blob:
        return None
    rest = blob[blob.index(b"NSString") + len(b"NSString"):]
    if b"+" not in rest:
        return None
    rest = rest[rest.index(b"+") + 1:]
    if not rest:
        return None
    n, start = rest[0], 1
    if n == 0x81:
        n, start = int.from_bytes(rest[1:3], "little"), 3
    elif n == 0x82:
        n, start = int.from_bytes(rest[1:5], "little"), 5
    return rest[start:start + n].decode("utf-8", "replace")


def _save(rowid: int) -> None:
    with open(STATE, "w") as f:
        f.write(str(rowid))


def inbound_pass() -> None:
    """Forward new incoming texts from phone numbers; a row is marked done only after the site accepted it."""
    with closing(sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)) as chat:
        if not os.path.exists(STATE):
            _save(chat.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])
            return
        with open(STATE) as f:
            last = int(f.read().strip() or 0)
        rows = chat.execute("SELECT m.ROWID, m.text, m.attributedBody, h.id FROM message m JOIN handle h ON h.ROWID = m.handle_id "
                            "WHERE m.is_from_me = 0 AND m.ROWID > ? ORDER BY m.ROWID LIMIT 200", (last,)).fetchall()
    for rowid, text, body, sender in rows:
        text = (text or decode_attributed_body(body) or "").replace("￼", "").strip()   # U+FFFC = attachment slot
        if PHONE.fullmatch(sender or "") and 0 < len(text) <= MAX_LEN:
            r = httpx.post(f"{SITE}/api/sms-inbound", headers=INBOUND_HEADERS, json={"phone": sender, "text": text},
                           timeout=20).raise_for_status()
            print(f"[{datetime.now():%H:%M:%S}] inbound …{sender[-4:]} {text[:20]!r} → {r.json().get('action')}", flush=True)
        _save(rowid)


if __name__ == "__main__":
    last_err = ""
    while True:
        try:
            jobs = httpx.get(f"{SITE}/api/sms-queue", headers={"X-Token": TOKEN}, timeout=20).raise_for_status().json()
            with open(OUTBOX, "a") as f:
                for j in jobs:
                    f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "text": j["text"],
                                        "to": [[j["phone"], SERVICE]]}) + "\n")
            if jobs:
                print(f"[{datetime.now():%H:%M:%S}] relayed {len(jobs)}", flush=True)
        except httpx.HTTPError as e:
            print(f"[{datetime.now():%H:%M:%S}] {e}", flush=True)
        try:
            inbound_pass()
            last_err = ""
        except Exception as e:   # noqa: BLE001 — inbound must never kill the outbound relay; retried next pass, logged once
            if str(e) != last_err:
                print(f"[{datetime.now():%H:%M:%S}] inbound: {e}", flush=True)
            last_err = str(e)
        time.sleep(5)
