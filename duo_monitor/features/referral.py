"""Referral: every customer gets a share link (<base_url>/?r=<code>); a friend who PAYS through it gives the referrer
+7 days on their newest active watch, or banks the days (referral_credit, by email) until their next watch activates,
and the friend's own paid watch gets the same +7 days (the invite's perk; shown on the landing page via /api/referral/check).
Free / admin activations never count as a paid referral. Self-referrals (same email or phone) are rejected.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from duo_monitor import db, hooks, i18n

log = logging.getLogger("duo_monitor.features.referral")
router = APIRouter()

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L: codes get read aloud and retyped
CODE_LEN = 6
REWARD_DAYS = 7


def setup(conn) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS referral_codes (
        sub_id TEXT PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        referrer_sub_id TEXT NOT NULL,
        referee_sub_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,              -- pending | credited | rejected
        reason TEXT,                       -- why rejected: self | unpaid:admin | unpaid:free
        created_at TEXT NOT NULL,
        credited_at TEXT
    );
    CREATE TABLE IF NOT EXISTS referral_credit (
        email TEXT PRIMARY KEY,            -- days owed to a referrer who had no active watch when the friend paid
        days INTEGER NOT NULL DEFAULT 0
    );
    """)


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def code_for(conn, sub_id: str) -> str:
    """The subscription's share code, created on first use."""
    for _ in range(20):
        row = conn.execute("SELECT code FROM referral_codes WHERE sub_id=?", (sub_id,)).fetchone()
        if row:
            return row["code"]
        try:
            with conn:
                conn.execute("INSERT INTO referral_codes (sub_id, code, created_at) VALUES (?, ?, ?)",
                             (sub_id, new_code(), db.iso(db.now())))
        except sqlite3.IntegrityError:   # code taken (or a concurrent insert for this sub): re-check and retry
            continue
    raise RuntimeError("could not allocate a referral code")


def share_url(code: str) -> str:
    from duo_monitor import web
    return f"{web.s.base_url.rstrip('/')}/?r={code}"


@router.get("/api/referral/link")
def referral_link(sub: str) -> dict:
    from duo_monitor import web
    if not db.get_subscription(web.conn, sub):
        raise HTTPException(404, "Unknown subscription")
    code = code_for(web.conn, sub)
    return {"code": code, "url": share_url(code), "reward_days": REWARD_DAYS}


@router.get("/api/referral/check")
def referral_check(code: str) -> dict:
    """Is this share code real? The landing page shows the friend's +days only for a valid invite."""
    from duo_monitor import web
    row = web.conn.execute("SELECT 1 FROM referral_codes WHERE code=?", (code.strip().upper()[:12],)).fetchone()
    return {"valid": bool(row), "days": REWARD_DAYS}


def _same_person(a, b) -> bool:
    return any(a[k] and a[k] == b[k] for k in ("email", "phone"))


@hooks.on("subscription.created")
def _on_created(conn, sub_id: str, ref: str = "", **_) -> None:
    code = (ref or "").strip().upper()
    row = conn.execute("SELECT sub_id FROM referral_codes WHERE code=?", (code,)).fetchone() if code else None
    if not row:
        return
    referrer, referee = db.get_subscription(conn, row["sub_id"]), db.get_subscription(conn, sub_id)
    if not referrer or not referee:
        return
    is_self = _same_person(referrer, referee)
    conn.execute("INSERT OR IGNORE INTO referrals (code, referrer_sub_id, referee_sub_id, status, reason, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (code, referrer["id"], sub_id, "rejected" if is_self else "pending", "self" if is_self else None,
                  db.iso(db.now())))
    conn.commit()


@hooks.on("subscription.activated")
def _on_activated(conn, sub_id: str, via: str = "", **_) -> None:
    if via == "stripe":
        _credit_referrer(conn, sub_id)
    else:   # comped / free-mode watches are not paid referrals
        conn.execute("UPDATE referrals SET status='rejected', reason=? WHERE referee_sub_id=? AND status='pending'",
                     (f"unpaid:{via}", sub_id))
        conn.commit()
    _apply_banked_credit(conn, sub_id)


def _extend(conn, sub, days: int) -> None:
    expires = datetime.fromisoformat(sub["expires_at"]) + timedelta(days=days)
    conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(expires), sub["id"]))


def _newest_active(conn, referrer):
    return conn.execute("SELECT * FROM subscriptions WHERE status='active' AND expires_at > ? AND (id=? OR email=?) "
                        "ORDER BY created_at DESC LIMIT 1",
                        (db.iso(db.now()), referrer["id"], referrer["email"])).fetchone()


def _credit_referrer(conn, referee_sub_id: str) -> None:
    ref = conn.execute("SELECT * FROM referrals WHERE referee_sub_id=? AND status='pending'", (referee_sub_id,)).fetchone()
    referrer = ref and db.get_subscription(conn, ref["referrer_sub_id"])
    if not referrer:
        return
    target = _newest_active(conn, referrer)
    with conn:
        cur = conn.execute("UPDATE referrals SET status='credited', credited_at=? WHERE id=? AND status='pending'",
                           (db.iso(db.now()), ref["id"]))
        if cur.rowcount != 1:   # already credited (webhook retried)
            return
        friend = db.get_subscription(conn, referee_sub_id)
        if friend and friend["status"] == "active" and friend["expires_at"]:
            _extend(conn, friend, REWARD_DAYS)
        if target:
            _extend(conn, target, REWARD_DAYS)
        else:
            conn.execute("INSERT INTO referral_credit (email, days) VALUES (?, ?) "
                         "ON CONFLICT(email) DO UPDATE SET days = days + excluded.days", (_contact(referrer), REWARD_DAYS))
    _notify(target or referrer, banked=target is None)


def _contact(sub) -> str | None:
    """Banked days are keyed by email, or by the phone for a text-only customer (the column keeps its old name)."""
    return sub["email"] or sub["phone"]


def _apply_banked_credit(conn, sub_id: str) -> None:
    sub = db.get_subscription(conn, sub_id)
    if not sub or sub["status"] != "active" or not _contact(sub):
        return
    row = conn.execute("SELECT days FROM referral_credit WHERE email=?", (_contact(sub),)).fetchone()
    if not row or row["days"] <= 0:
        return
    with conn:
        cur = conn.execute("UPDATE referral_credit SET days=0 WHERE email=? AND days=?", (_contact(sub), row["days"]))
        if cur.rowcount == 1:
            _extend(conn, sub, row["days"])


def _message(lang: str, banked: bool) -> tuple[str, str]:
    """→ (subject, body)."""
    return (i18n.t("referral.subject", lang, days=REWARD_DAYS),
            i18n.t("referral.bodyBanked" if banked else "referral.body", lang, days=REWARD_DAYS))


def _notify(sub, banked: bool) -> None:
    """Background thread: SMS/email must never slow the Stripe webhook.
    Banked = the referrer has no active watch, possibly because they texted STOP: email only, never text them."""
    from duo_monitor import web
    subject, body = _message(sub["lang"], banked)
    if banked:
        sub = {**dict(sub), "phone": None}
    notifier = web.Notifier(web.s, web.conn)

    def send() -> None:
        try:
            notifier.alert(sub, body, subject, email_only=True)
        except Exception:  # noqa: BLE001
            log.exception("referral notice to %s failed", sub["id"])
    threading.Thread(target=send, name="referral-notify", daemon=True).start()


@hooks.footer
def _footer(sub, kind: str, lang: str) -> str:
    from duo_monitor import web
    if kind != "welcome" or not web.s.payments_on:   # free mode: no activation is paid, so the promise would be false
        return ""
    url = share_url(code_for(web.conn, sub["id"]))
    return i18n.t("referral.footer", lang, days=REWARD_DAYS, url=url)
