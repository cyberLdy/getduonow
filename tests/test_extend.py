import importlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from duo_monitor import db, hooks, signing

WATCHES = [("R300", "Riverside Mall", "MK1E4LL/A", "iPhone Duo 256GB Star White"),
           ("R300", "Riverside Mall", "MK1F4LL/A", "iPhone Duo 256GB Night Sky"),
           ("R030", "Lakeside", "MK1E4LL/A", "iPhone Duo 256GB Star White")]


def _sub_of(location: str) -> str:
    from urllib.parse import parse_qs, urlsplit
    return parse_qs(urlsplit(location).query)["sub"][0]


def _load(tmp_path, monkeypatch, stripe_key=""):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WATCH_DAYS", "7")
    monkeypatch.setenv("PRICE_CENTS", "499")
    for name in ("BASIC_AUTH", "ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    if stripe_key:
        monkeypatch.setenv("STRIPE_SECRET_KEY", stripe_key)
    else:
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    import duo_monitor.web as w
    w = importlib.reload(w)
    welcomed: list[str] = []
    monkeypatch.setattr(w, "_welcome", welcomed.append)   # no real SMS / email from tests
    return w, welcomed


@pytest.fixture
def web(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


def _old_sub(w, expires_in: timedelta) -> str:
    sid = db.create_subscription(w.conn, email="me@example.com", phone="+15015550123", carrier="att",
                                 watches=WATCHES, lang="zh")
    db.activate(w.conn, sid, 14)
    w.conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(db.now() + expires_in), sid))
    w.conn.commit()
    return sid


def _path(sid: str) -> str:
    return signing.link("", "extend", sid)


def _exp(w, sid: str) -> datetime:
    return datetime.fromisoformat(db.get_subscription(w.conn, sid)["expires_at"])


def test_page_shows_watch_and_bad_sig_is_404(web):
    w, _ = web
    sid = _old_sub(w, timedelta(days=3))
    c = TestClient(w.app)
    r = c.get(_path(sid) + "?lang=en")
    assert r.status_code == 200
    assert "Lakeside" in r.text and "iPhone Duo 256GB Night Sky" in r.text and "Watching until" in r.text
    assert ">Extend<" in r.text and 'href="/"' in r.text   # payments off: plain button; add-stores link
    assert "续期" in c.get(_path(sid), headers={"accept-language": "zh-CN,zh;q=0.9"}).text
    assert c.get(f"/extend/{sid}/0000000000").status_code == 404
    assert c.get(_path("nosuchsub")).status_code == 404


@pytest.mark.parametrize("left", [timedelta(days=3), timedelta(days=-2)])
def test_free_extend_chains_expiry_and_retires_old(web, left):
    w, welcomed = web
    old = _old_sub(w, left)
    before = db.now()
    r = TestClient(w.app).post(_path(old), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/done?sub=")
    new = _sub_of(r.headers["location"])
    sub = db.get_subscription(w.conn, new)
    assert sub["status"] == "active" and (sub["email"], sub["phone"], sub["carrier"], sub["lang"]) == \
        ("me@example.com", "+15015550123", "att", "zh")
    assert [tuple(x)[1:] for x in db.get_watches(w.conn, new)] == [tuple(x)[1:] for x in db.get_watches(w.conn, old)]
    base = max(before + left, before)   # old pass still running → days stack on top; already over → from now
    assert abs(_exp(w, new) - (base + timedelta(days=7))) < timedelta(seconds=5)
    assert db.get_subscription(w.conn, old)["status"] == "expired"
    assert w.conn.execute("SELECT old_sub_id FROM extensions WHERE new_sub_id=?", (new,)).fetchone()[0] == old
    assert welcomed == [new]
    assert set(db.watch_targets(w.conn)) == {"R300", "R030"}


def test_old_link_extends_the_current_pass_not_the_retired_one(web):
    w, _ = web
    old = _old_sub(w, timedelta(days=3))
    c = TestClient(w.app)
    first = _sub_of(c.post(_path(old), follow_redirects=False).headers["location"])
    second = _sub_of(c.post(_path(old), follow_redirects=False).headers["location"])
    assert abs(_exp(w, second) - (_exp(w, old) + timedelta(days=14))) < timedelta(seconds=5)
    assert [db.get_subscription(w.conn, x)["status"] for x in (old, first, second)] == ["expired", "expired", "active"]


def test_paid_extend_redirects_to_checkout_and_chains_on_activation(tmp_path, monkeypatch):
    w, _ = _load(tmp_path, monkeypatch, stripe_key="sk_test_x")
    calls = []
    monkeypatch.setattr(w, "create_checkout", lambda sid, email, watches, title, lang="en": calls.append((sid, email, watches, title))
                        or "https://checkout.example/s")
    old = _old_sub(w, timedelta(days=3))
    c = TestClient(w.app)
    assert "Extend 7 days · $4.99" in c.get(_path(old) + "?lang=en").text
    r = c.post(_path(old), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://checkout.example/s"
    new, email, watches, title = calls[0]
    assert email == "me@example.com" and len(watches) == 3 and title == "GetDuoNow +7 days"
    assert db.get_subscription(w.conn, new)["status"] == "pending"
    assert db.get_subscription(w.conn, old)["status"] == "active"   # unpaid: old pass untouched
    db.activate(w.conn, new, 7)                                       # what the Stripe webhook does
    hooks.emit("subscription.activated", w.conn, sub_id=new, via="stripe")
    assert abs(_exp(w, new) - (_exp(w, old) + timedelta(days=7))) < timedelta(seconds=5)
    assert db.get_subscription(w.conn, old)["status"] == "expired"


def test_plain_subscription_activation_is_untouched(web):
    w, _ = web
    sid = _old_sub(w, timedelta(days=3))
    exp = _exp(w, sid)
    hooks.emit("subscription.activated", w.conn, sub_id=sid, via="free")
    assert _exp(w, sid) == exp and db.get_subscription(w.conn, sid)["status"] == "active"


def test_extension_is_not_realerted_for_a_cell_the_old_pass_already_got(web):
    w, _ = web
    old = _old_sub(w, timedelta(days=3))
    t0 = db.now() - timedelta(minutes=10)
    for store, part in (("R300", "MK1E4LL/A"), ("R030", "MK1E4LL/A")):
        db.upsert_availability(w.conn, store=store, part=part, available=True, store_name="x", quote="Today", at=t0)
    db.record_alert(w.conn, sub_id=old, store="R300", part="MK1E4LL/A", ok=True, detail="sent", at=t0 + timedelta(minutes=1))
    new = _sub_of(TestClient(w.app).post(_path(old), follow_redirects=False).headers["location"])
    since = db.iso(t0)
    assert new not in [s["id"] for s in db.subscribers_to_alert(w.conn, "R300", "MK1E4LL/A", since)]   # already told
    assert new in [s["id"] for s in db.subscribers_to_alert(w.conn, "R030", "MK1E4LL/A", since)]       # never told


def test_takeover_keeps_days_another_activation_handler_added_first(web, monkeypatch):
    w, _ = web

    def bonus(conn, sub_id, **_):   # e.g. referral credit, registered ahead of extend
        exp = datetime.fromisoformat(db.get_subscription(conn, sub_id)["expires_at"]) + timedelta(days=2)
        conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(exp), sub_id))
        conn.commit()

    monkeypatch.setitem(hooks._handlers, "subscription.activated", [bonus, *hooks._handlers["subscription.activated"]])
    old = _old_sub(w, timedelta(days=3))
    new = _sub_of(TestClient(w.app).post(_path(old), follow_redirects=False).headers["location"])
    assert abs(_exp(w, new) - (_exp(w, old) + timedelta(days=9))) < timedelta(seconds=5)


def test_unsubscribed_pass_page_does_not_claim_a_future_end_date(web):
    w, _ = web
    sid = _old_sub(w, timedelta(days=3))
    db.expire(w.conn, sid)
    text = TestClient(w.app).get(_path(sid) + "?lang=en").text
    assert "Ended" in text and str(_exp(w, sid).year) not in text.split("Ended", 1)[1][:20]
