"""Whole-journey dry run: sign up → pay (signed Stripe webhook) → welcome → restock alert → sold out → live log →
"got it" story → share link → friend pays through it (both +7 days) → unsubscribe.
Nothing leaves the process: Stripe is a signed local webhook, Apple is a fake client, messages are recorded.
Run `pytest tests/test_journey.py -s` to print every message a customer would receive."""
import hashlib
import hmac
import importlib
import json
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from duo_monitor.apple import Cell

PARTS = {"MK1E4LL/A": "iPhone Duo 256GB Star White", "MK1G4LL/A": "iPhone Duo 512GB Star White"}
CATALOG = [{"family": "iphone-duo", "family_name": "iPhone Duo", "rank": 0, "part": p, "name": n, "size": n.split()[2],
            "color": "Star White", "price": 1999.0} for p, n in PARTS.items()]
WHSEC = "whsec_journey"
TB_KEY = "tb_journey"


class FakeApple:
    """Pickup stock we control: `stock` = parts in stock at R300."""
    def __init__(self):
        self.stock: set[str] = set()

    def availability(self, store, parts):
        return [Cell(store=store, store_name="Riverside Mall", city="Springfield", part=p, product=PARTS[p],
                     available=p in self.stock, quote="Today" if p in self.stock else "") for p in parts]


@pytest.fixture
def world(tmp_path, monkeypatch):
    for k, v in {"DB_PATH": str(tmp_path / "t.db"), "STRIPE_SECRET_KEY": "sk_test_journey", "STRIPE_WEBHOOK_SECRET": WHSEC,
                 "BASE_URL": "https://duo.test", "SMS_ENABLED": "1", "ADMIN_TOKEN": "adm",
                 "TEXTBELT_KEY": TB_KEY}.items():
        monkeypatch.setenv(k, v)
    import stripe
    import duo_monitor.web as w
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    w.inbox = []                                            # every message a customer would get: (channel, to, text)
    monkeypatch.setattr(w.Notifier, "sms", lambda self, to, text: (w.inbox.append(("sms", to, text)), (True, "sent: sms test"))[1])
    monkeypatch.setattr(w.Notifier, "email", lambda self, to, subject, text, headers=None:
                        (w.inbox.append(("email", to, f"{subject}\n{text}")), (True, "sent: email test"))[1])
    w.checkouts = []
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: (
        w.checkouts.append(kw), type("S", (), {"id": f"cs_{kw['metadata']['sub_id']}", "url": "https://checkout.test/"})())[1])
    return w


def settle():
    for t in threading.enumerate():
        if t.name in ("welcome", "referral-notify"):
            t.join(5)


def pay(w, c, sub_id):
    """Stripe's checkout.session.completed, signed the way Stripe signs it."""
    payload = json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed", "data": {"object": {
        "id": f"cs_{sub_id}", "object": "checkout.session", "payment_status": "paid", "metadata": {"sub_id": sub_id}}}})
    ts = str(int(time.time()))
    sig = hmac.new(WHSEC.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    assert c.post("/stripe/webhook", content=payload, headers={"stripe-signature": f"t={ts},v1={sig}"}).status_code == 200
    settle()


def reply(c, phone, text):
    """The customer texts back; Textbelt forwards it signed: HMAC-SHA256(key, timestamp + raw body)."""
    raw, ts = json.dumps({"fromNumber": phone, "text": text}), str(int(time.time()))
    sig = hmac.new(TB_KEY.encode(), (ts + raw).encode(), hashlib.sha256).hexdigest()
    r = c.post("/textbelt/inbound", content=raw, headers={"x-textbelt-timestamp": ts, "x-textbelt-signature": sig})
    assert r.status_code == 200, r.text
    return r.json()["action"]


def signup(c, email, phone, lang, ref=""):
    r = c.post("/api/subscribe", json={"email": email, "phone": phone, "sms_opt_in": bool(phone), "carrier": "unlocked",
                                       "lang": lang, "ref": ref, "stores": [{"store": "R300", "store_name": "Riverside Mall",
                                                                             "parts": list(PARTS)}]})
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["checkout_url"], r.text
    return r.json()["id"]


def got(w, kind_to, needle):
    return [m for m in w.inbox if m[1] == kind_to and needle in m[2]]


def test_whole_journey(world, capsys):
    w, c = world, TestClient(world.app)
    apple = FakeApple()
    poll = lambda: w.poller.poll_once(w.conn, apple, w.Notifier(w.s, w.conn))   # noqa: E731

    # ① – ④ sign up in Chinese with SMS → Stripe page in Chinese → paid → welcome by email + text
    alice = signup(c, "alice@example.com", "501 555 0101", "zh")
    assert w.checkouts[-1]["locale"] == "zh" and f"sig={w.signing.sign(alice, 'watch')}" in w.checkouts[-1]["success_url"]
    assert w.db.get_subscription(w.conn, alice)["status"] == "pending"
    pay(w, c, alice)
    assert w.db.get_subscription(w.conn, alice)["status"] == "active"
    assert got(w, "+15015550101", "回复 YES") and got(w, "alice@example.com", "开始帮你盯货了")
    assert got(w, "+15015550101", "Riverside Mall 的 iPhone Duo 256GB Star White, 512GB Star White")
    log_api = f"/api/watch/{alice}/{w.signing.sign(alice, 'watch')}"
    assert c.get(log_api).json()["status"] == "active" and c.get(log_api).json()["sms"] == {"state": "waiting", "last4": "0101"}
    # texts start only after her YES (in Chinese: 是)
    assert reply(c, "+15015550101", "是") == "confirm"
    assert got(w, "+15015550101", "已开启 ✓") and c.get(log_api).json()["sms"]["state"] == "on"

    # restock at Riverside → alert in her language (text + email with the order link) → sold out → log shows both
    poll()
    apple.stock = {"MK1E4LL/A"}
    poll()
    assert got(w, "+15015550101", "有货！") and got(w, "alice@example.com", "https://www.apple.com/shop/product/MK1E4LL/A")
    apple.stock = set()
    poll()
    kinds = [e["kind"] for e in c.get(log_api).json()["events"]]
    assert {"start", "alert", "restock"} <= set(kinds)

    # "Got it?" link from the alert email → story with consent → public stories page
    got_url = w.signing.link("", "got", alice)
    assert any(got_url in m[2] for m in w.inbox if m[1] == "alice@example.com")
    c.post(got_url, data={"got": "1", "name": "Alice", "city": "Springfield", "quote": "Got it in 3 minutes!", "consent": "1", "lang": "zh"})
    assert "Got it in 3 minutes!" in c.get("/stories").text and c.get("/api/stories/summary").json()["got"] == 1

    # share → friend arrives with the code (valid invite), signs up in Spanish, pays → both +7 days, Alice notified
    link = c.get("/api/referral/link", params={"sub": alice}).json()
    assert link["url"].startswith("https://duo.test/?r=")
    code = link["code"]
    assert c.get("/api/referral/check", params={"code": code}).json()["valid"] is True
    alice_before = datetime.fromisoformat(w.db.get_subscription(w.conn, alice)["expires_at"])
    bob = signup(c, "bob@example.com", "", "es", ref=code)
    assert w.checkouts[-1]["locale"] == "es"
    pay(w, c, bob)
    bob_sub, alice_sub = w.db.get_subscription(w.conn, bob), w.db.get_subscription(w.conn, alice)
    bob_days = (datetime.fromisoformat(bob_sub["expires_at"]) - datetime.fromisoformat(bob_sub["created_at"])).total_seconds() / 86400
    assert round(bob_days) == w.s.watch_days + 7
    assert round((datetime.fromisoformat(alice_sub["expires_at"]) - alice_before).total_seconds() / 86400) == 7
    assert got(w, "alice@example.com", "好友通过你的链接加入了")
    assert got(w, "bob@example.com", "GetDuoNow")                                  # Bob's welcome (email only, no phone)

    # unsubscribe from the signed link in the email → watch ends, no more alerts
    unsub = w.signing.link("", "unsub", bob)
    assert c.post(unsub).status_code == 200 and w.db.get_subscription(w.conn, bob)["status"] == "expired"
    n = len(w.inbox)
    apple.stock = {"MK1G4LL/A"}
    poll()
    assert not any(m[1] == "bob@example.com" for m in w.inbox[n:])                 # Bob unsubscribed: nothing
    assert any(m[1] == "alice@example.com" for m in w.inbox[n:])                   # Alice still watching

    texts = [m[2] for m in w.inbox if m[0] == "sms"]    # SMS consent covers restock alerts + the YES confirmation only
    assert texts and all("有货！" in x or "回复 YES" in x or "已开启 ✓" in x for x in texts), texts

    with capsys.disabled():
        print("\n---- what customers received ----")
        for channel, to, text in w.inbox:
            print(f"[{channel} → {to}] {text}\n")


def test_success_page_confirms_payment_without_the_webhook(world, monkeypatch):
    """A text-only customer pays; the webhook never arrives. The success page asks Stripe itself, starts the watch once,
    keeps the email Checkout collected as the backup channel, and sends the confirmation text."""
    import stripe
    w, c = world, TestClient(world.app)
    sub_id = signup(c, "", "5015550199", "en")
    sig = w.signing.sign(sub_id, "watch")
    calls = []
    paid = {"value": False}
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", lambda cs: (calls.append(cs), {
        "id": cs, "payment_status": "paid" if paid["value"] else "unpaid", "metadata": {"sub_id": sub_id},
        "customer_details": {"email": "Buyer@Example.com"}})[1])
    assert c.get(f"/api/watch/{sub_id}/{sig}").json()["status"] == "pending"          # not paid yet
    paid["value"] = True
    assert c.get(f"/api/watch/{sub_id}/{sig}").json()["status"] == "pending"          # throttled: no 2nd Stripe call within 3 s
    w._confirm_at.clear()
    r = c.get(f"/api/watch/{sub_id}/{sig}").json()
    settle()
    assert r["status"] == "active" and r["paid"] is True and calls == [f"cs_{sub_id}"] * 2
    assert w.db.get_subscription(w.conn, sub_id)["email"] == "buyer@example.com"
    to = lambda ch, who: [m for m in w.inbox if m[:2] == (ch, who)]
    assert len(to("sms", "+15015550199")) == 1 and len(to("email", "buyer@example.com")) == 1
    pay(w, c, sub_id)                                                                   # the late webhook changes nothing
    assert len(to("sms", "+15015550199")) == 1


def test_text_only_watch_joins_the_checks_after_yes_in_any_language(world):
    """Free (admin) watch, phone only: not checked, not texted until YES; 네 (Korean) also switches its texts to Korean."""
    w, c = world, TestClient(world.app)
    apple = FakeApple()
    r = c.post("/api/subscribe", headers={"x-admin-token": "adm"}, json={
        "email": "", "phone": "5015550177", "sms_opt_in": True, "carrier": "unlocked", "lang": "en", "ref": "",
        "stores": [{"store": "R300", "store_name": "Riverside Mall", "parts": ["MK1E4LL/A"]}]})
    sub_id = r.json()["id"]
    settle()
    assert got(w, "+15015550177", "reply YES to start text alerts for iPhone Duo 256GB Star White at Riverside Mall")
    assert w.db.watch_targets(w.conn) == {}                                     # not checked yet
    assert reply(c, "+15015550177", "hello") == "ignored"
    assert reply(c, "+15015550177", "네!") == "confirm"
    assert got(w, "+15015550177", "설정 완료 ✓")
    assert w.db.get_subscription(w.conn, sub_id)["lang"] == "ko"
    assert w.db.watch_targets(w.conn) == {"R300": {"MK1E4LL/A"}}
    apple.stock = {"MK1E4LL/A"}
    w.poller.poll_once(w.conn, apple, w.Notifier(w.s, w.conn))
    assert len([m for m in w.inbox if m[1] == "+15015550177"]) == 3             # confirm, confirmed, restock alert
    assert reply(c, "+15015550177", "yes") == "start"                          # nothing left to confirm: plain START
    assert reply(c, "+15015550177", "네") == "start"                           # in any language
