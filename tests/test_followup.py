from datetime import timedelta

import pytest

from duo_monitor import hooks, signing
from duo_monitor.features import followup

WATCHES = [("R300", "Riverside", "MK1E4LL/A", "iPhone Duo 256GB Star White"),
           ("R030", "Lakeside", "MK1E4LL/A", "iPhone Duo 256GB Star White")]


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    import importlib

    import duo_monitor.web as web
    web = importlib.reload(web)
    web.sent = []

    def fake_alert(self, sub, text, subject, **_):
        web.sent.append((sub["id"], text, subject))
        return True, "stub"
    monkeypatch.setattr(web.Notifier, "alert", fake_alert)
    monkeypatch.setattr(followup, "_last_scan", float("-inf"))
    return web


def make_sub(w, *, status="active", lang="en", days=30, created_ago=timedelta(0)):
    sid = w.db.create_subscription(w.conn, email="a@example.com", phone=None, carrier="unlocked", watches=WATCHES, lang=lang)
    if status != "pending":
        w.db.activate(w.conn, sid, days)
    if status == "expired":
        w.db.expire(w.conn, sid)
    w.conn.execute("UPDATE subscriptions SET created_at=? WHERE id=?", (w.db.iso(w.db.now() - created_ago), sid))
    w.conn.commit()
    return sid


def set_expiry(w, sid, at):
    w.conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (w.db.iso(at), sid))
    w.conn.commit()


def alert(w, sid, at, ok=True, store="R300", part="MK1E4LL/A"):
    w.db.record_alert(w.conn, sub_id=sid, store=store, part=part, ok=ok, detail="", at=at)


def test_hooks_are_registered():
    assert followup.on_soldout in hooks._handlers["cell.soldout"]
    assert followup.on_poll_pass in hooks._handlers["poll.pass"]
    assert followup.remember_activation in hooks._handlers["subscription.activated"]


def test_soldout_note_only_to_alerted_active_subs_once(w):
    now = w.db.now()
    since = w.db.iso(now - timedelta(minutes=5))
    en, zh = make_sub(w), make_sub(w, lang="zh")
    old_stretch, failed, expired, other_cell, pending = (make_sub(w), make_sub(w), make_sub(w, status="expired"),
                                                         make_sub(w), make_sub(w, status="pending"))
    for sid in (en, zh, expired, pending):
        alert(w, sid, now)
    alert(w, old_stretch, now - timedelta(hours=2))
    alert(w, failed, now, ok=False)
    alert(w, other_cell, now, store="R030")
    for _ in range(2):
        followup.on_soldout(w.conn, store="R300", part="MK1E4LL/A", store_name="Riverside", since=since, minutes=0)
    assert sorted(sid for sid, _, _ in w.sent) == sorted([en, zh])
    texts = dict((sid, text) for sid, text, _ in w.sent)
    assert texts[en] == ("GetDuoNow: that restock at Riverside sold out in 1 min. "
                         "We're still watching — order the moment the next alert arrives.")
    assert texts[zh] == "GetDuoNow：Riverside 这批 1 分钟就卖完了，我们继续帮你盯，下次收到提醒请马上下单。"


def test_expiry_reminder_once_inside_48h_window(w):
    now = w.db.now()
    soon, later, expired = make_sub(w), make_sub(w), make_sub(w, status="expired")
    set_expiry(w, soon, now + timedelta(hours=47))
    set_expiry(w, later, now + timedelta(hours=72))
    set_expiry(w, expired, now + timedelta(hours=10))
    followup.scan(w.conn, now)
    followup.scan(w.conn, now + timedelta(minutes=30))
    assert [sid for sid, _, _ in w.sent] == [soon]
    link = signing.link(w.s.base_url, "extend", soon)
    assert w.sent[0][1] == f"GetDuoNow: your watch ends in 2 days. Extend {w.s.watch_days} days: {link}"
    followup.scan(w.conn, now + timedelta(hours=30))          # `later` now has 42 h left
    assert [sid for sid, _, _ in w.sent] == [soon, later]
    set_expiry(w, soon, now + timedelta(days=14, hours=20))  # extended: its new end gets its own reminder
    w.sent.clear()
    followup.scan(w.conn, now + timedelta(days=14))
    assert [sid for sid, text, _ in w.sent if "ends in 1 day." in text] == [soon]


def test_heartbeat_weekly_skipped_when_alerted_recently(w):
    now = w.db.now()
    quiet, zh, alerted = (make_sub(w, created_ago=timedelta(days=8)), make_sub(w, lang="zh", created_ago=timedelta(days=8)),
                          make_sub(w, created_ago=timedelta(days=8)))
    young = make_sub(w, created_ago=timedelta(days=3))
    paid_today = make_sub(w, created_ago=timedelta(days=20))   # created long ago, activated just now
    followup.remember_activation(w.conn, sub_id=paid_today, via="stripe")
    alert(w, alerted, now - timedelta(days=2))
    followup.scan(w.conn, now)
    followup.scan(w.conn, now + timedelta(hours=1))
    assert sorted(sid for sid, _, _ in w.sent) == sorted([quiet, zh])
    texts = dict((sid, text) for sid, text, _ in w.sent)
    assert texts[quiet] == "GetDuoNow: still watching 2 model/store picks for you. No restock yet — we check every 30 seconds."
    assert texts[zh] == "解放双手：还在帮你盯 2 个型号/门店组合，暂未到货，每 30 秒查一次。"
    followup.scan(w.conn, now + timedelta(days=3))    # alert now 5 days old: its quiet week stays quiet
    assert len(w.sent) == 2
    w.sent.clear()
    followup.scan(w.conn, now + timedelta(days=7, hours=1))    # week 2 for the 8-day-old subs, week 1 for the others
    assert sorted(sid for sid, _, _ in w.sent) == sorted([quiet, zh, alerted, young, paid_today])


def test_no_heartbeat_next_to_expiry_reminder(w):
    """Extending on the reminder (48 h before the end) gives a pass whose week-2 heartbeat lands in its own last 48 h."""
    now = w.db.now()
    sid = make_sub(w)
    followup.remember_activation(w.conn, sub_id=sid, via="stripe")
    set_expiry(w, sid, now + timedelta(days=16))
    followup.scan(w.conn, now + timedelta(days=14, hours=1))
    followup.scan(w.conn, now + timedelta(days=14, hours=2))
    assert [text.split(" ")[1:4] for _, text, _ in w.sent] == [["your", "watch", "ends"]]


def test_poll_pass_scan_is_throttled(w):
    first = make_sub(w)
    set_expiry(w, first, w.db.now() + timedelta(hours=20))
    followup.on_poll_pass(w.conn, stats={})
    assert [sid for sid, _, _ in w.sent] == [first]
    second = make_sub(w)
    set_expiry(w, second, w.db.now() + timedelta(hours=20))
    followup.on_poll_pass(w.conn, stats={})
    assert len(w.sent) == 1                               # inside the 10-minute window: no scan
    followup._last_scan -= followup.SCAN_EVERY + 1
    followup.on_poll_pass(w.conn, stats={})
    assert [sid for sid, _, _ in w.sent] == [first, second]


def test_soldout_end_to_end_through_poller(w):
    from duo_monitor import poller
    from duo_monitor.apple import Cell

    class FakeApple:
        available = True

        def availability(self, store, parts):
            return [Cell(store, f"Store {store}", "Springfield", p, "iPhone Duo", self.available, "Today") for p in parts]

    sid = make_sub(w)
    apple = FakeApple()
    poller.poll_once(w.conn, apple, w.Notifier(w.s, w.conn))       # in stock: the subscriber is alerted
    apple.available = False
    poller.poll_once(w.conn, apple, w.Notifier(w.s, w.conn))       # sold out: the follow-up goes out once
    poller.poll_once(w.conn, apple, w.Notifier(w.s, w.conn))
    mine = sorted((s, t) for s, t, _ in w.sent if t.startswith("GetDuoNow: that restock"))
    assert mine == [(sid, f"GetDuoNow: that restock at Store {store} sold out in 1 min. "
                          "We're still watching — order the moment the next alert arrives.") for store in ("R030", "R300")]
