"""Follow-ups that keep subscribers informed without spamming:
  - "sold out in N min" after a restock they were alerted about (cell.soldout),
  - one reminder when the watch ends within 48 h, with a signed extend link (poll.pass scan),
  - a weekly "still watching" heartbeat, skipped for a week in which they already got an alert (poll.pass scan).
followup_log is claimed BEFORE sending, so each follow-up goes out at most once even if two processes race.
Only active subscriptions are ever messaged.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta

from .. import db, hooks, i18n, signing

SCAN_EVERY = 600                    # seconds between poll.pass scans (a pass runs every ~30 s)
REMIND_WITHIN = timedelta(hours=48)
WEEK = timedelta(days=7)
_last_scan = float("-inf")          # time.monotonic() of the last scan

def setup(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS followup_log (sub_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
                 "sent_at TEXT NOT NULL, PRIMARY KEY (sub_id, kind, key))")
    conn.commit()


def _claim(conn, sub_id: str, kind: str, key: str) -> bool:
    """True only the first time (sub, kind, key) is seen; the row is written before the message goes out."""
    cur = conn.execute("INSERT OR IGNORE INTO followup_log (sub_id, kind, key, sent_at) VALUES (?, ?, ?, ?)",
                       (sub_id, kind, key, db.iso(db.now())))
    conn.commit()
    return cur.rowcount == 1


def _send(conn, sub, kind: str, **fmt) -> None:
    from duo_monitor import web
    lang = sub["lang"]
    web.Notifier(web.s, conn).alert(sub, i18n.t(f"followup.{kind}.body", lang, **fmt), i18n.t(f"followup.{kind}.subject", lang),
                                    email_only=True)


@hooks.on("subscription.activated")
def remember_activation(conn, sub_id: str, **_) -> None:
    """Heartbeat weeks count from activation (core stores only created_at / expires_at, and extends move expires_at)."""
    _claim(conn, sub_id, "activated", "")


@hooks.on("cell.soldout")
def on_soldout(conn, store: str, part: str, store_name: str, since: str, minutes: int, **_) -> None:
    subs = conn.execute("SELECT * FROM subscriptions WHERE status = 'active' AND id IN ("
                        "SELECT sub_id FROM alerts WHERE store = ? AND part = ? AND ok = 1 AND sent_at >= ?)",
                        (store, part, since)).fetchall()
    for sub in subs:
        if _claim(conn, sub["id"], "soldout", f"{store}|{part}|{since}"):
            _send(conn, sub, "soldout", store=store_name, m=max(1, int(minutes)))


@hooks.on("poll.pass")
def on_poll_pass(conn, **_) -> None:
    global _last_scan
    if time.monotonic() - _last_scan < SCAN_EVERY:
        return
    _last_scan = time.monotonic()
    scan(conn, db.now())


def scan(conn, now: datetime) -> None:
    from duo_monitor import web
    for sub in conn.execute("SELECT * FROM subscriptions WHERE status = 'active'").fetchall():
        left = datetime.fromisoformat(sub["expires_at"]) - now
        if left <= timedelta(0):
            continue        # past its end; expire_old flips it at the next pass
        _expiry_reminder(conn, sub, left, web.s)
        _heartbeat(conn, sub, now, ending=left <= REMIND_WITHIN)


def _expiry_reminder(conn, sub, left: timedelta, s) -> None:
    # keyed by expires_at: an extended watch gets its own reminder before its new end
    if left > REMIND_WITHIN or not _claim(conn, sub["id"], "expiry", sub["expires_at"]):
        return
    n = max(1, math.ceil(left / timedelta(days=1)))
    _send(conn, sub, "expiry", n=n, days="day" if n == 1 else "days", watch_days=s.watch_days,
          link=signing.link(s.base_url, "extend", sub["id"]))


def _heartbeat(conn, sub, now: datetime, ending: bool) -> None:
    row = conn.execute("SELECT sent_at FROM followup_log WHERE sub_id = ? AND kind = 'activated'", (sub["id"],)).fetchone()
    week = int((now - datetime.fromisoformat(row["sent_at"] if row else sub["created_at"])) / WEEK)
    # the week is claimed even when skipped, so a quiet week never turns into a mid-week ping later
    if week < 1 or not _claim(conn, sub["id"], "heartbeat", str(week)):
        return
    if ending:      # the last 48 h belong to the expiry reminder (an extension bought on it ends 2 weeks + 2 days out)
        return
    if conn.execute("SELECT 1 FROM alerts WHERE sub_id = ? AND ok = 1 AND sent_at >= ?",
                    (sub["id"], db.iso(now - WEEK))).fetchone():
        return
    n = conn.execute("SELECT COUNT(*) FROM watches WHERE sub_id = ?", (sub["id"],)).fetchone()[0]
    _send(conn, sub, "heartbeat", n=n)
