"""No-restock guarantee: a watch that ends without a single delivered alert is extended free once (same length);
if it still sees no restock, the customer can reply to any email for a full refund (manual, in Stripe).

Runs on poll.pass (throttled): picks active subscriptions expiring within LEAD with zero ok alerts and no prior
free extension, pushes expires_at out by watch_days, emails the customer once.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .. import db, hooks, i18n

LEAD = timedelta(minutes=15)   # act shortly before expiry so the watch never lapses
THROTTLE_S = 300
_last_run = 0.0


def setup(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS guarantee (sub_id TEXT PRIMARY KEY, extended_at TEXT NOT NULL, days INTEGER NOT NULL)")
    conn.commit()


def _copy(lang: str, days: int) -> tuple[str, str]:
    return i18n.t("guarantee.subject", lang), i18n.t("guarantee.body", lang, days=days)


def due(conn, now: datetime) -> list:
    """Active subs expiring within LEAD that never got a delivered alert and weren't extended for free yet."""
    return conn.execute(
        "SELECT s.* FROM subscriptions s WHERE s.status='active' AND s.expires_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.sub_id=s.id AND a.ok=1) "
        "AND NOT EXISTS (SELECT 1 FROM guarantee g WHERE g.sub_id=s.id)", (db.iso(now + LEAD),)).fetchall()


def extend_free(conn, sub, days: int, now: datetime) -> str:
    new_exp = max(datetime.fromisoformat(sub["expires_at"]), now) + timedelta(days=days)
    with conn:
        conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(new_exp), sub["id"]))
        conn.execute("INSERT INTO guarantee (sub_id, extended_at, days) VALUES (?, ?, ?)", (sub["id"], db.iso(now), days))
    return db.iso(new_exp)


@hooks.on("poll.pass")
def run(conn, **_) -> None:
    global _last_run
    if time.time() - _last_run < THROTTLE_S:
        return
    _last_run = time.time()
    from duo_monitor import web
    days = web.s.watch_days
    for sub in due(conn, db.now()):
        extend_free(conn, sub, days, db.now())
        subject, body = _copy(sub["lang"], days)
        web.Notifier(web.s, conn).alert(sub, body, subject, email_only=True)
        hooks.emit("subscription.guarantee_extended", conn, sub_id=sub["id"], days=days)
