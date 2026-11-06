"""SQLite storage: subscriptions (contact + carrier), their per-store watches, last-seen availability, sent alerts."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,              -- pending | active | expired
    email TEXT,
    phone TEXT,
    carrier TEXT NOT NULL,             -- unlocked | att | tmobile | verizon (pickup stock is the same hardware)
    checkout_session TEXT,
    lang TEXT NOT NULL DEFAULT 'en'    -- en | zh: language of alerts
);
CREATE TABLE IF NOT EXISTS watches (
    sub_id TEXT NOT NULL,
    store TEXT NOT NULL,
    store_name TEXT NOT NULL,
    part TEXT NOT NULL,
    product TEXT NOT NULL,
    PRIMARY KEY (sub_id, store, part)
);
CREATE TABLE IF NOT EXISTS availability (
    store TEXT NOT NULL,
    part TEXT NOT NULL,
    available INTEGER NOT NULL,
    since TEXT NOT NULL,               -- when the cell last flipped to its current state
    store_name TEXT,
    quote TEXT,
    PRIMARY KEY (store, part)
);
CREATE TABLE IF NOT EXISTS sms_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    phone TEXT NOT NULL,
    text TEXT NOT NULL,
    pulled_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id TEXT NOT NULL,
    store TEXT NOT NULL,
    part TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="microseconds")   # second precision cannot order an alert and a restock in the same second


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL: readers never block the writer and vice versa (web process + poller thread + webhooks share one file);
    # busy_timeout: a writer waits up to 5 s for another writer instead of failing with "database is locked"
    conn.execute("PRAGMA busy_timeout=5000")      # first, so the one-time switch to WAL waits for a lock instead of failing
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    if "lang" not in cols:   # DBs created before alerts were localized
        conn.execute("ALTER TABLE subscriptions ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
        conn.commit()
    if "sms_ok_at" not in cols:   # text alerts start only once the customer replies YES (double opt-in, 11-04)
        conn.execute("ALTER TABLE subscriptions ADD COLUMN sms_ok_at TEXT")
        # watches from before the change were signed up under single opt-in: they keep getting texts
        conn.execute("UPDATE subscriptions SET sms_ok_at = created_at WHERE phone IS NOT NULL")
        conn.commit()
    return conn


def create_subscription(conn, *, email: str | None, phone: str | None, carrier: str,
                        watches: list[tuple[str, str, str, str]], lang: str = "en") -> str:
    """watches: (store, store_name, part, product)."""
    sub_id = uuid.uuid4().hex
    with conn:
        conn.execute("INSERT INTO subscriptions (id, created_at, status, email, phone, carrier, lang) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                     (sub_id, iso(now()), email, phone, carrier, lang))
        conn.executemany("INSERT OR IGNORE INTO watches (sub_id, store, store_name, part, product) VALUES (?, ?, ?, ?, ?)",
                         [(sub_id, *w) for w in watches])
    return sub_id


def activate(conn, sub_id: str, days: int, checkout_session: str | None = None) -> bool:
    cur = conn.execute("UPDATE subscriptions SET status='active', expires_at=?, checkout_session=COALESCE(?, checkout_session) "
                       "WHERE id=? AND status='pending'", (iso(now() + timedelta(days=days)), checkout_session, sub_id))
    conn.commit()
    return cur.rowcount == 1


def expire(conn, sub_id: str) -> bool:
    cur = conn.execute("UPDATE subscriptions SET status='expired' WHERE id=? AND status!='expired'", (sub_id,))
    conn.commit()
    return cur.rowcount == 1


def get_subscription(conn, sub_id: str):
    return conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()


def get_watches(conn, sub_id: str) -> list:
    return conn.execute("SELECT * FROM watches WHERE sub_id=? ORDER BY store, product", (sub_id,)).fetchall()


def set_checkout_session(conn, sub_id: str, session_id: str) -> None:
    conn.execute("UPDATE subscriptions SET checkout_session=? WHERE id=?", (session_id, sub_id))
    conn.commit()


def expire_old(conn, at: datetime) -> int:
    cur = conn.execute("UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at < ?", (iso(at),))
    conn.commit()
    return cur.rowcount


def watch_targets(conn) -> dict[str, set[str]]:
    """store -> parts that at least one active subscription watches and can be told about: an email, or a number that
    replied YES (a text-only watch joins the checks once its owner confirms)."""
    targets: dict[str, set[str]] = {}
    for r in conn.execute("SELECT w.store, w.part FROM watches w JOIN subscriptions s ON s.id = w.sub_id "
                          "WHERE s.status = 'active' AND (COALESCE(s.email, '') != '' OR s.sms_ok_at IS NOT NULL)"):
        targets.setdefault(r["store"], set()).add(r["part"])
    return targets


def get_availability(conn, store: str, part: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM availability WHERE store=? AND part=?", (store, part)).fetchone()


def upsert_availability(conn, *, store: str, part: str, available: bool, store_name: str, quote: str, at: datetime,
                        commit: bool = True) -> str:
    """Record the latest state; returns the `since` timestamp of the current state.
    commit=False leaves the transaction open so a caller can write a whole pass and commit once."""
    prev = get_availability(conn, store, part)
    since = prev["since"] if prev and bool(prev["available"]) == available else iso(at)
    conn.execute("INSERT INTO availability (store, part, available, since, store_name, quote) VALUES (?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(store, part) DO UPDATE SET available=excluded.available, since=excluded.since, "
                 "store_name=excluded.store_name, quote=excluded.quote",
                 (store, part, int(available), since, store_name, quote))
    if commit:
        conn.commit()
    return since


MAX_ATTEMPTS = 3     # sends per subscription per in-stock stretch of a cell; a channel that keeps failing is not retried forever


def confirm_sms(conn, phone: str) -> list[str]:
    """The number replied YES: its active watches that were waiting start texting. → their ids."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM subscriptions WHERE phone=? AND status='active' AND sms_ok_at IS NULL", (phone,))]
    if ids:
        conn.executemany("UPDATE subscriptions SET sms_ok_at=? WHERE id=?", [(iso(now()), i) for i in ids])
        conn.commit()
    return ids


def subscribers_to_alert(conn, store: str, part: str, since: str) -> list[sqlite3.Row]:
    """Active subscriptions watching (store, part) still owed an alert for the in-stock stretch that began at `since`:
    none delivered yet and fewer than MAX_ATTEMPTS tries so far (row has watch.product)."""
    return conn.execute(
        "SELECT s.*, w.product FROM subscriptions s JOIN watches w ON w.sub_id = s.id "
        "WHERE s.status = 'active' AND w.store = ? AND w.part = ? AND NOT EXISTS ("
        "  SELECT 1 FROM alerts a WHERE a.sub_id = s.id AND a.store = w.store AND a.part = w.part AND a.ok = 1 AND a.sent_at >= ?) "
        "AND (SELECT COUNT(*) FROM alerts a WHERE a.sub_id = s.id AND a.store = w.store AND a.part = w.part "
        "     AND a.sent_at >= ?) < ?",
        (store, part, since, since, MAX_ATTEMPTS)).fetchall()


def record_alert(conn, *, sub_id: str, store: str, part: str, ok: bool, detail: str, at: datetime) -> None:
    conn.execute("INSERT INTO alerts (sub_id, store, part, sent_at, ok, detail) VALUES (?, ?, ?, ?, ?, ?)",
                 (sub_id, store, part, iso(at), int(ok), detail[:500]))
    conn.commit()


def enqueue_sms(conn, phone: str, text: str) -> None:
    conn.execute("INSERT INTO sms_queue (created_at, phone, text) VALUES (?, ?, ?)", (iso(now()), phone, text))
    conn.commit()


def pull_sms(conn) -> list[dict]:
    """Hand out every unpulled SMS once (marks them pulled)."""
    with conn:
        rows = conn.execute("SELECT id, phone, text FROM sms_queue WHERE pulled_at IS NULL ORDER BY id").fetchall()
        conn.executemany("UPDATE sms_queue SET pulled_at=? WHERE id=?", [(iso(now()), r["id"]) for r in rows])
    return [{"phone": r["phone"], "text": r["text"]} for r in rows]
