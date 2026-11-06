"""Footers (unsubscribe / referral / "got one?" links) go to email only: texts opt out by "Reply STOP" and every link
costs SMS segments."""
import importlib
import re
import threading

import pytest

from duo_monitor import hooks, messages, signing
from duo_monitor.apple import Cell

PART = "MK1E4LL/A"
URL = re.compile(r"https?://\S+")


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("BASE_URL", "https://getduonow.test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")      # payments on: the referral footer is live too
    for k in ("BASIC_AUTH", "ACCESS_KEY", "SMS_OUTBOX", "SMS_PULL_TOKEN", "TWILIO_ACCOUNT_SID", "RESEND_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    import duo_monitor.web as web
    web = importlib.reload(web)
    web.out = []
    monkeypatch.setattr(web.Notifier, "sms", lambda self, to, text: (web.out.append(("sms", text)), (True, "sent: sms test"))[1])
    monkeypatch.setattr(web.Notifier, "email", lambda self, to, subject, text, headers=None:
                        (web.out.append(("email", text)), (True, "sent: email test"))[1])
    return web


def active_sub(w) -> str:
    sid = w.db.create_subscription(w.conn, email="a@example.com", phone="+15015550123", carrier="unlocked",
                                   watches=[("R300", "Riverside", PART, "iPhone Duo 256GB Star White")])
    w.db.activate(w.conn, sid, days=14)
    w.db.confirm_sms(w.conn, "+15015550123")       # replied YES to the confirmation text
    return sid


def test_alert_sms_has_only_the_order_link_and_email_keeps_the_footers(w):
    from duo_monitor import poller

    class Apple:
        def availability(self, store, parts):
            return [Cell(store, "Riverside", "Springfield", p, "iPhone Duo", True, "Today") for p in parts]

    sid = active_sub(w)
    poller.poll_once(w.conn, Apple(), w.Notifier(w.s, w.conn))
    sent = dict(w.out)
    assert URL.findall(sent["sms"]) == [messages.order_url(PART)] and "Reply STOP" in sent["sms"]
    assert f"Unsubscribe: {signing.link(w.s.base_url, 'unsub', sid)}" in sent["email"]
    assert f"Got one? Tell us: {signing.link(w.s.base_url, 'got', sid)}" in sent["email"]
    assert sent["email"].count("Unsubscribe:") == 1                       # appended once, not at the call site too


def test_welcome_sms_has_no_links_and_email_keeps_the_footers(w):
    sid = active_sub(w)
    w._welcome(sid)
    for t in threading.enumerate():
        if t.name == "welcome":
            t.join(5)
    sent = dict(w.out)
    assert URL.findall(sent["sms"]) == [] and "Reply STOP" in sent["sms"]
    assert len(sent["sms"]) <= 160 and all(k in sent["sms"] for k in ("GetDuoNow", "Msg & data rates may apply", "HELP"))
    assert f"Unsubscribe: {signing.link(w.s.base_url, 'unsub', sid)}" in sent["email"]
    assert "Share your link" in sent["email"]


def test_footer_can_opt_into_sms(monkeypatch):
    monkeypatch.setattr(hooks, "_footers", [])
    hooks.footer(lambda sub, kind, lang: "email only")
    hooks.footer(channels=("email", "sms"))(lambda sub, kind, lang: "both")
    sub = {"id": "x"}
    assert hooks.footer_text(sub, "alert", "en", "sms") == "\nboth"
    assert hooks.footer_text(sub, "alert", "en", "email") == "\nemail only\nboth"
    assert hooks.footer_text(sub, "alert", "en") == "\nemail only\nboth"       # channel defaults to email
