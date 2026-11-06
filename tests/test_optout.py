import base64
import hashlib
import hmac
import importlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WATCH = [("R300", "Riverside", "MK1E4LL/A", "iPhone Duo 256GB Star White")]
PHONE = "+15015550123"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SMS_PULL_TOKEN", "tok")
    monkeypatch.setenv("BASE_URL", "https://getduonow.test")
    for k in ("STRIPE_SECRET_KEY", "ACCESS_KEY", "BASIC_AUTH", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_FROM"):
        monkeypatch.delenv(k, raising=False)
    import duo_monitor.web as w
    return importlib.reload(w)


def new_sub(w, phone=PHONE, lang="en", active=True):
    sid = w.db.create_subscription(w.conn, email="me@example.com", phone=phone, carrier="unlocked", watches=WATCH, lang=lang)
    if active:
        w.db.activate(w.conn, sid, 14)
    return sid


def status(w, sid):
    return w.db.get_subscription(w.conn, sid)["status"]


def queued(w):
    return [tuple(r) for r in w.conn.execute("SELECT phone, text FROM sms_queue ORDER BY id")]


def unsub_path(sid):
    from duo_monitor import signing
    return signing.link("", "unsub", sid)


# ---- opt-out helpers ------------------------------------------------------------------------------------------------

def test_opt_out_phone_expires_pending_and_active_on_that_phone_only(web):
    from duo_monitor.features import optout
    a, p, other = new_sub(web), new_sub(web, active=False), new_sub(web, phone="+15015550199")
    assert optout.opt_out_phone(web.conn, PHONE) == 2
    assert (status(web, a), status(web, p), status(web, other)) == ("expired", "expired", "active")
    assert optout.opt_out_phone(web.conn, PHONE) == 0


def test_opt_out_sub(web):
    from duo_monitor.features import optout
    sid = new_sub(web)
    assert optout.opt_out_sub(web.conn, sid) is True
    assert optout.opt_out_sub(web.conn, sid) is False
    assert status(web, sid) == "expired"


# ---- unsubscribe page / one-click ---------------------------------------------------------------------------------

def test_unsub_page_confirms_then_expires(web):
    sid, c = new_sub(web), TestClient(web.app)
    r = c.get(unsub_path(sid))
    assert r.status_code == 200 and 'method="post"' in r.text and "Unsubscribe" in r.text
    assert status(web, sid) == "active"                  # GET alone (mail scanners) must not unsubscribe
    r = c.post(unsub_path(sid))
    assert r.status_code == 200 and "You're unsubscribed" in r.text
    assert status(web, sid) == "expired"


def test_unsub_page_language(web):
    """The watch's own language (picked at signup) beats the browser's; ?lang= beats both."""
    sid, zh_sid, c = new_sub(web), new_sub(web, phone="+15015550124", lang="zh"), TestClient(web.app)
    assert "确认退订" in c.get(unsub_path(sid) + "?lang=zh").text
    assert "确认退订" in c.get(unsub_path(zh_sid), headers={"Accept-Language": "en-US"}).text
    assert "Stop alerts?" in c.get(unsub_path(sid), headers={"Accept-Language": "zh-CN,zh;q=0.9"}).text
    assert 'dir="rtl"' in c.get(unsub_path(sid) + "?lang=ar").text
    assert "已退订" in c.post(unsub_path(sid) + "?lang=zh").text


def test_rfc8058_one_click_post(web):
    sid = new_sub(web)
    r = TestClient(web.app).post(unsub_path(sid), content=b"List-Unsubscribe=One-Click",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and status(web, sid) == "expired"


@pytest.mark.parametrize("method", ["get", "post"])
def test_bad_signature_is_404(web, method):
    sid, c = new_sub(web), TestClient(web.app)
    assert getattr(c, method)(f"/unsub/{sid}/{'0' * 20}").status_code == 404
    assert getattr(c, method)(unsub_path("nope")).status_code == 404       # valid sig, unknown subscription
    assert status(web, sid) == "active"


def test_email_headers_and_footer_carry_the_signed_link(web):
    from duo_monitor import hooks, signing
    sid = new_sub(web)
    sub, link = web.db.get_subscription(web.conn, sid), signing.link("https://getduonow.test", "unsub", sid)
    h = hooks.email_headers(sub)
    assert h["List-Unsubscribe"] == f"<{link}>" and h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert f"Unsubscribe: {link}" in hooks.footer_text(sub, "alert", "en")
    assert f"退订：{link}" in hooks.footer_text(sub, "welcome", "zh")


# ---- inbound SMS (relay) -------------------------------------------------------------------------------------------

def inbound(w, text, phone="(501) 555-0123", token="tok"):
    return TestClient(w.app).post("/api/sms-inbound", json={"phone": phone, "text": text}, headers={"X-Token": token})


def test_sms_inbound_requires_token(web):
    assert inbound(web, "STOP", token="wrong").status_code == 401
    assert TestClient(web.app).post("/api/sms-inbound", json={"phone": PHONE, "text": "STOP"}).status_code == 401


@pytest.mark.parametrize("text", ["STOP", " stop ", "Unsubscribe", "QUIT", "stopall", "cancel", "End", "Stop."])
def test_sms_stop_expires_all_and_confirms(web, text):
    a, b = new_sub(web), new_sub(web, active=False)
    assert inbound(web, text).json() == {"action": "stop"}
    assert (status(web, a), status(web, b)) == ("expired", "expired")
    assert queued(web) == [(PHONE, "GetDuoNow: you're unsubscribed, no more alerts. To watch again, start a new watch on our site.")]


@pytest.mark.parametrize("text", ["退订", "取消"])
def test_sms_chinese_stop_replies_in_chinese(web, text):
    sid = new_sub(web)
    assert inbound(web, text).json() == {"action": "stop"}
    assert status(web, sid) == "expired" and "已退订" in queued(web)[0][1]


def test_sms_stop_reply_follows_subscription_language(web):
    new_sub(web, lang="zh")
    inbound(web, "STOP")
    assert queued(web)[0][1].startswith("GetDuoNow：已退订")


def test_sms_help_and_ignored(web):
    sid = new_sub(web)
    assert inbound(web, "help").json() == {"action": "help"}
    assert inbound(web, "帮助").json() == {"action": "help"}
    assert inbound(web, "thanks!").json() == {"action": "ignored"}
    assert inbound(web, "STOP", phone="hello@icloud.com").json() == {"action": "ignored"}
    texts = [t for _, t in queued(web)]
    assert len(texts) == 2 and "Reply STOP" in texts[0] and "https://getduonow.test" in texts[0] and "回复 STOP 退订" in texts[1]
    assert status(web, sid) == "active"


@pytest.mark.parametrize("text", ["Yes", "help", "Stop"])
def test_sms_relay_never_replies_to_a_non_customer(web, text):
    """The relay reads the operator's personal Messages: friends texting "yes" must not get GetDuoNow texts."""
    new_sub(web)
    assert inbound(web, text, phone="+15015550999").json() == {"action": "ignored"}
    assert queued(web) == [] and web.conn.execute("SELECT COUNT(*) FROM sms_consent").fetchone()[0] == 0


@pytest.mark.parametrize("text", ["START", "y", "Yes", "UNSTOP"])
def test_sms_start_records_consent_without_reactivating(web, text):
    sid = new_sub(web)
    inbound(web, "STOP")
    assert inbound(web, text).json() == {"action": "start"}
    assert tuple(web.conn.execute("SELECT phone, status FROM sms_consent").fetchone()) == (PHONE, "on")
    assert status(web, sid) == "expired"
    assert queued(web)[-1] == (PHONE, "GetDuoNow: confirmed, alerts on.")


# ---- Twilio webhook ------------------------------------------------------------------------------------------------

def twilio_post(w, params, token="twtok", url="https://getduonow.test/twilio/inbound", sig=None):
    data = url + "".join(k + v for k, v in sorted(params.items()))
    good = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return TestClient(w.app).post("/twilio/inbound", data=params, headers={"X-Twilio-Signature": sig or good})


@pytest.fixture
def twweb(monkeypatch, web):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twtok")
    return importlib.reload(web)


def test_twilio_bad_signature_is_403(twweb):
    params = {"From": PHONE, "Body": "STOP", "MessageSid": "SM1"}
    sid = new_sub(twweb)
    assert twilio_post(twweb, params, sig="bad").status_code == 403
    assert twilio_post(twweb, params, token="other").status_code == 403
    assert status(twweb, sid) == "active"


def test_twilio_disabled_when_no_auth_token(web):
    assert twilio_post(web, {"From": PHONE, "Body": "STOP"}).status_code == 403


def test_twilio_stop_expires_and_returns_empty_twiml(twweb):
    sid = new_sub(twweb)
    r = twilio_post(twweb, {"From": PHONE, "Body": "Stop", "MessageSid": "SM1", "To": "+15015550000"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert r.text.endswith("<Response/>") and status(twweb, sid) == "expired"
    assert queued(twweb) == []                            # Twilio replies itself; nothing goes to the relay queue


def test_twilio_help_and_start_reply_in_twiml(twweb):
    new_sub(twweb, lang="zh")
    assert "<Message>GetDuoNow：解放双手" in twilio_post(twweb, {"From": PHONE, "Body": "HELP"}).text
    r = twilio_post(twweb, {"From": PHONE, "Body": "start"})
    assert "<Response><Message>GetDuoNow：已确认" in r.text
    assert twilio_post(twweb, {"From": PHONE, "Body": "hi"}).text.endswith("<Response/>")


def test_twilio_signature_matches_twilio_docs_example():
    """Test vector from twilio-python's RequestValidator tests."""
    from duo_monitor.features.optout import twilio_signature
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030",
              "To": "+18005551212"}
    assert twilio_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2",
                            list(params.items())) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


# ---- Textbelt inbound -----------------------------------------------------------------------------------------------

@pytest.fixture
def tbweb(monkeypatch, web):
    monkeypatch.setenv("TEXTBELT_KEY", "tbkey")
    w = importlib.reload(web)
    w.texts = []
    monkeypatch.setattr(w.Notifier, "sms", lambda self, to, text: (w.texts.append((to, text)), (True, "sent: sms test"))[1])
    return w


def textbelt_post(w, payload: dict, key="tbkey", age=0):
    import json
    import time
    raw, ts = json.dumps(payload), str(int(time.time()) - age)
    sig = hmac.new(key.encode(), (ts + raw).encode(), hashlib.sha256).hexdigest()
    return TestClient(w.app).post("/textbelt/inbound", content=raw, headers={
        "content-type": "application/json", "X-textbelt-timestamp": ts, "X-textbelt-signature": sig})


def test_textbelt_reply_must_be_signed_and_fresh(tbweb):
    sid = new_sub(tbweb)
    assert textbelt_post(tbweb, {"fromNumber": PHONE, "text": "STOP"}, key="other").status_code == 403
    assert textbelt_post(tbweb, {"fromNumber": PHONE, "text": "STOP"}, age=3600).status_code == 403
    assert status(tbweb, sid) == "active"


def test_textbelt_help_is_answered_and_stop_is_left_to_textbelt(tbweb):
    sid = new_sub(tbweb)
    r = textbelt_post(tbweb, {"textId": "1", "fromNumber": "5015550123", "text": "HELP", "data": ""})
    assert r.json() == {"action": "help"} and tbweb.texts[-1][0] == PHONE and "GetDuoNow" in tbweb.texts[-1][1]
    sent = len(tbweb.texts)
    assert textbelt_post(tbweb, {"fromNumber": PHONE, "text": "Stop"}).json() == {"action": "stop"}
    assert status(tbweb, sid) == "expired" and len(tbweb.texts) == sent     # Textbelt confirms STOP itself


# ---- Mac relay inbound poller ---------------------------------------------------------------------------------------

def attributed(text: str) -> bytes:
    raw = text.encode()
    n = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85"
            b"\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + n + raw + b"\x86\x84\x02iI\x01")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    chat = tmp_path / "chat.db"
    db = sqlite3.connect(chat)
    db.executescript("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);"
                     "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, handle_id INT, is_from_me INT);"
                     "INSERT INTO handle VALUES (1, '+15015550123'), (2, 'friend@icloud.com');"
                     "INSERT INTO message VALUES (1, 'old STOP', NULL, 1, 0);")
    db.commit()
    monkeypatch.setenv("SITE", "https://getduonow.test/")
    monkeypatch.setenv("SMS_PULL_TOKEN", "tok")
    monkeypatch.setenv("OUTBOX", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("CHAT_DB", str(chat))
    monkeypatch.delenv("ACCESS_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("sms_relay", Path(__file__).parent.parent / "scripts" / "sms_relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []

    class Resp:
        def raise_for_status(self):
            return self

        def json(self):
            return {"action": "stop"}

    monkeypatch.setattr(mod.httpx, "post", lambda url, headers, json, timeout: sent.append((url, headers["X-Token"], json)) or Resp())
    return mod, db, sent, tmp_path


def test_decode_attributed_body_short_and_long(relay):
    decode = relay[0].decode_attributed_body
    assert decode(attributed("退订")) == "退订"
    assert decode(attributed("x" * 300)) == "x" * 300
    assert decode(None) is None and decode(b"no string here") is None
    assert decode(b"\x84\x84\x08NSString\x01\x94\x84\x01+") is None      # truncated blob: no crash


def test_relay_forwards_only_new_phone_replies(relay):
    mod, db, sent, tmp = relay
    mod.inbound_pass()                                     # first run: start from the newest row, no replay
    assert sent == [] and (tmp / "sms_relay_inbound.rowid").read_text() == "1"
    db.executescript("INSERT INTO message VALUES (2, NULL, X'" + attributed("Stop").hex() + "', 1, 0);"
                     "INSERT INTO message VALUES (3, 'STOP', NULL, 2, 0);"          # email sender: skipped
                     "INSERT INTO message VALUES (4, 'STOP', NULL, 1, 1);"          # our own outgoing text: skipped
                     "INSERT INTO message VALUES (5, 'a long personal message here', NULL, 1, 0);")   # never leaves the Mac
    db.commit()
    mod.inbound_pass()
    assert sent == [("https://getduonow.test/api/sms-inbound", "tok", {"phone": "+15015550123", "text": "Stop"})]
    assert (tmp / "sms_relay_inbound.rowid").read_text() == "5"
    mod.inbound_pass()
    assert len(sent) == 1


def test_relay_does_not_advance_past_a_failed_post(relay, monkeypatch):
    mod, db, sent, tmp = relay
    mod.inbound_pass()
    db.execute("INSERT INTO message VALUES (2, 'STOP', NULL, 1, 0)")
    db.commit()

    def down(*a, **k):
        raise mod.httpx.ConnectError("down")

    ok = mod.httpx.post
    monkeypatch.setattr(mod.httpx, "post", down)
    with pytest.raises(mod.httpx.HTTPError):
        mod.inbound_pass()
    assert (tmp / "sms_relay_inbound.rowid").read_text() == "1"
    monkeypatch.setattr(mod.httpx, "post", ok)
    mod.inbound_pass()
    assert len(sent) == 1 and (tmp / "sms_relay_inbound.rowid").read_text() == "2"
