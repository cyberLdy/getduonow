import importlib
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

CATALOG = [{"part": "MK1E4LL/A", "name": "iPhone Duo 256GB Star White", "size": "256GB", "color": "Star White", "price": 1999.0}]
ADMIN = {"X-Admin-Token": "adm"}


@pytest.fixture
def web(tmp_path, monkeypatch):
    """Payments on (Stripe stubbed), admin token set, Notifier stubbed."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("ADMIN_TOKEN", "adm")
    monkeypatch.setenv("BASE_URL", "https://getduonow.test")
    for k in ("BASIC_AUTH", "ACCESS_KEY", "SMS_OUTBOX", "SMS_PULL_TOKEN", "TWILIO_ACCOUNT_SID", "RESEND_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    import duo_monitor.web as w
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    monkeypatch.setattr(w, "create_checkout", lambda sub_id, email, watches, title="", lang="en": f"https://stripe.test/{sub_id}")
    w.sent = []
    monkeypatch.setattr(w.Notifier, "alert", lambda self, sub, text, subject, **_: (w.sent.append((sub["id"], text, subject)), (True, "stub"))[1])
    return w


@pytest.fixture
def ref():
    from duo_monitor.features import referral
    return referral


def body(email, phone="", ref_code=""):
    return {"email": email, "phone": phone, "sms_opt_in": bool(phone), "carrier": "unlocked", "ref": ref_code,
            "stores": [{"store": "R300", "store_name": "Riverside Mall", "parts": ["MK1E4LL/A"]}]}


def subscribe(c, email, phone="", ref_code="", admin=False):
    r = c.post("/api/subscribe", json=body(email, phone, ref_code), headers=ADMIN if admin else {})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def pay(w, c, monkeypatch, sub_id):
    """Simulate Stripe's checkout.session.completed webhook for sub_id."""
    import stripe
    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": f"cs_{sub_id}", "payment_status": "paid", "metadata": {"sub_id": sub_id}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)
    assert c.post("/stripe/webhook", content=b"{}").status_code == 200


def expires(w, sub_id):
    return datetime.fromisoformat(w.db.get_subscription(w.conn, sub_id)["expires_at"])


def notices(w):
    for t in threading.enumerate():
        if t.name == "referral-notify":
            t.join(5)
    return [s for s in w.sent if "joined with your link" in s[1] or "通过你的链接加入了" in s[1]]


def test_codes_use_unambiguous_alphabet(ref):
    codes = {ref.new_code() for _ in range(300)}
    assert all(len(c) == 6 and set(c) <= set(ref.ALPHABET) for c in codes)
    assert not set("".join(codes)) & set("0O1IL")


def test_code_collision_retries_until_unique(web, ref, monkeypatch):
    a, b = "sub-a", "sub-b"
    seq = iter(["AAAAAA", "AAAAAA", "BBBBBB"])
    monkeypatch.setattr(ref, "new_code", lambda: next(seq))
    assert ref.code_for(web.conn, a) == "AAAAAA"
    assert ref.code_for(web.conn, b) == "BBBBBB"      # first draw collided with a's code
    assert ref.code_for(web.conn, a) == "AAAAAA"      # stable per subscription


def test_link_endpoint(web):
    c = TestClient(web.app)
    assert c.get("/api/referral/link", params={"sub": "nope"}).status_code == 404
    sub = subscribe(c, "a@example.com", admin=True)
    r = c.get("/api/referral/link", params={"sub": sub}).json()
    assert r["url"] == f"https://getduonow.test/?r={r['code']}" and r["reward_days"] == 7
    assert c.get("/api/referral/link", params={"sub": sub}).json()["code"] == r["code"]


def test_self_referral_rejected(web, monkeypatch):
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", phone="501 555 0123", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    before = expires(web, referrer)
    same_email = subscribe(c, "a@example.com", ref_code=code)
    same_phone = subscribe(c, "other@example.com", phone="+1 501 555 0123", ref_code=code.lower())
    rows = web.conn.execute("SELECT status, reason FROM referrals").fetchall()
    assert [tuple(r) for r in rows] == [("rejected", "self"), ("rejected", "self")]
    pay(web, c, monkeypatch, same_email)
    pay(web, c, monkeypatch, same_phone)
    assert expires(web, referrer) == before and notices(web) == []


def test_paid_referee_credits_referrer_exactly_seven_days(web, monkeypatch):
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    before = expires(web, referrer)
    friend = subscribe(c, "friend@example.com", ref_code=code)
    assert web.conn.execute("SELECT status FROM referrals WHERE referee_sub_id=?", (friend,)).fetchone()[0] == "pending"
    pay(web, c, monkeypatch, friend)
    pay(web, c, monkeypatch, friend)                  # webhook retry: activate() refuses, no double credit
    assert expires(web, referrer) - before == timedelta(days=7)
    row = web.conn.execute("SELECT status, credited_at FROM referrals WHERE referee_sub_id=?", (friend,)).fetchone()
    assert row["status"] == "credited" and row["credited_at"]
    [(to, text, _)] = notices(web)
    assert to == referrer and "your watch got +7 days" in text


def test_credit_banked_when_referrer_inactive_then_applied(web, monkeypatch):
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    assert c.post(f"/api/admin/expire/{referrer}", headers=ADMIN).json() == {"expired": True}
    pay(web, c, monkeypatch, subscribe(c, "friend@example.com", ref_code=code))
    days = lambda: web.conn.execute("SELECT days FROM referral_credit WHERE email='a@example.com'").fetchone()[0]  # noqa: E731
    assert days() == 7
    assert "saved for your next watch" in notices(web)[0][1]
    again = subscribe(c, "a@example.com", admin=True)  # any activation (even comped) collects the banked days
    left = expires(web, again) - datetime.now(expires(web, again).tzinfo)
    assert abs(left - timedelta(days=web.s.watch_days + 7)) < timedelta(minutes=1)
    assert days() == 0


def test_banked_notice_never_texts_an_inactive_referrer(web, monkeypatch):
    """No active watch may mean the referrer texted STOP: the banked-credit notice goes by email only."""
    contacts = {}   # message text → (phone, email) it was sent to
    monkeypatch.setattr(web.Notifier, "alert",
                        lambda self, sub, text, subject, **_: (web.sent.append((sub["id"], text, subject)),
                                                          contacts.__setitem__(text, (sub["phone"], sub["email"])),
                                                          (True, "stub"))[2])
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", phone="501 555 0123", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    assert c.post(f"/api/admin/expire/{referrer}", headers=ADMIN).json() == {"expired": True}
    pay(web, c, monkeypatch, subscribe(c, "friend@example.com", ref_code=code))
    [(to, text, _)] = notices(web)
    assert to == referrer and "saved for your next watch" in text
    assert contacts[text] == (None, "a@example.com")


def test_admin_activation_does_not_credit(web):
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    before = expires(web, referrer)
    subscribe(c, "friend@example.com", ref_code=code, admin=True)
    assert expires(web, referrer) == before and notices(web) == []
    assert tuple(web.conn.execute("SELECT status, reason FROM referrals").fetchone()) == ("rejected", "unpaid:admin")
    assert web.conn.execute("SELECT COUNT(*) FROM referral_credit").fetchone()[0] == 0


def test_unknown_ref_is_ignored(web):
    c = TestClient(web.app)
    subscribe(c, "friend@example.com", ref_code="ZZZZZZ")
    assert web.conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0] == 0


def test_welcome_footer_has_share_link(web, ref):
    c = TestClient(web.app)
    sub = web.db.get_subscription(web.conn, subscribe(c, "a@example.com", admin=True))
    code = ref.code_for(web.conn, sub["id"])
    assert ref._footer(sub, "welcome", "en") == f"Share your link — every friend who joins gives you +7 days: https://getduonow.test/?r={code}"
    assert ref._footer(sub, "welcome", "zh").startswith("分享你的专属链接，每位好友加入你多 7 天：")
    assert ref._footer(sub, "alert", "en") == ""


def test_paying_friend_also_gets_the_bonus_days(web, monkeypatch):
    """The friend who joins through a link gets the same +7 days on their paid watch (the invite's "discount")."""
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    friend, plain = subscribe(c, "b@example.com", ref_code=code), subscribe(c, "c@example.com")
    pay(web, c, monkeypatch, friend)
    pay(web, c, monkeypatch, plain)
    assert round((expires(web, friend) - expires(web, plain)).total_seconds() / 86400) == 7   # paid ms apart: +7 days
    assert c.get("/api/referral/check", params={"code": code.lower()}).json() == {"valid": True, "days": 7}
    assert c.get("/api/referral/check", params={"code": "ZZZZZZ"}).json() == {"valid": False, "days": 7}


def test_self_or_unpaid_referral_gives_the_friend_nothing(web, monkeypatch):
    c = TestClient(web.app)
    referrer = subscribe(c, "a@example.com", admin=True)
    code = c.get("/api/referral/link", params={"sub": referrer}).json()["code"]
    me_again, comped = subscribe(c, "a@example.com", ref_code=code), subscribe(c, "d@example.com", ref_code=code, admin=True)
    pay(web, c, monkeypatch, me_again)
    days = lambda s: (expires(web, s) - datetime.fromisoformat(web.db.get_subscription(web.conn, s)["created_at"])).days  # noqa: E731
    assert days(me_again) <= 14 and days(comped) <= 14
