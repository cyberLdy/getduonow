import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

CATALOG = [{"part": "MK1E4LL/A", "name": "iPhone Duo 256GB Star White", "size": "256GB", "color": "Star White", "price": 1999.0},
           {"part": "MK1F4LL/A", "name": "iPhone Duo 256GB Night Sky", "size": "256GB", "color": "Night Sky", "price": 1999.0}]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    import importlib

    import duo_monitor.web as w
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    return w


def body(**kw):
    b = {"email": "me@example.com", "phone": "501 555 0123", "sms_opt_in": True, "carrier": "unlocked",
         "stores": [{"store": "R300", "store_name": "Riverside Mall", "parts": ["MK1E4LL/A", "MK1F4LL/A"]},
                    {"store": "R030", "store_name": "Lakeside", "parts": ["MK1E4LL/A"]}]}
    b.update(kw)
    return b


def test_split_name():
    import duo_monitor.web as w
    assert w._split("iPhone Duo 1TB Night Sky") == ("1TB", "Night Sky")
    assert w._gb("2TB") > w._gb("512GB")


@pytest.mark.parametrize("raw,want", [("501 555 0123", "+15015550123"), ("+1 (501) 555-0123", "+15015550123")])
def test_normalize_us_mobile(web, raw, want):
    assert web.normalize_phone(raw) == want


@pytest.mark.parametrize("fn,raw", [("normalize_phone", "555-0123"), ("normalize_email", "nope")])
def test_bad_contacts_rejected(web, fn, raw):
    with pytest.raises(HTTPException):
        getattr(web, fn)(raw)


def test_free_mode_subscription_saves_per_store_models(web):
    r = TestClient(web.app).post("/api/subscribe", json=body())
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert web.db.watch_targets(web.conn) == {"R300": {"MK1E4LL/A", "MK1F4LL/A"}, "R030": {"MK1E4LL/A"}}
    sub = web.conn.execute("SELECT email, phone, carrier FROM subscriptions").fetchone()
    assert tuple(sub) == ("me@example.com", "+15015550123", "unlocked")


def test_text_first_email_is_the_backup(web):
    """Either channel is enough: a mobile number (texts are the fastest) or an email; neither is rejected."""
    c = TestClient(web.app)
    assert c.post("/api/subscribe", json=body(email="")).status_code == 200                      # text only
    assert c.post("/api/subscribe", json=body(phone="", sms_opt_in=False)).status_code == 200    # email only
    assert c.post("/api/subscribe", json=body(email="", phone="", sms_opt_in=False)).status_code == 400
    assert c.post("/api/subscribe", json=body(email="", sms_opt_in=False)).status_code == 400   # number without consent
    assert [r[0] for r in web.conn.execute("SELECT email FROM subscriptions ORDER BY rowid")] == [None, "me@example.com"]


def test_phone_is_kept_only_with_an_explicit_sms_opt_in(web):
    c = TestClient(web.app)
    c.post("/api/subscribe", json=body(sms_opt_in=False))      # number typed, box unticked: no texts, number not kept
    c.post("/api/subscribe", json=body())
    assert [r[0] for r in web.conn.execute("SELECT phone FROM subscriptions ORDER BY rowid")] == [None, "+15015550123"]


def test_sms_opt_in_without_a_number_or_email_is_rejected(web):
    assert TestClient(web.app).post("/api/subscribe", json=body(phone="", email="")).status_code == 400


@pytest.mark.parametrize("signup,kept", [(True, "+15015550123"), (False, None)])
def test_sms_signup_can_open_before_texts_go_live(web, monkeypatch, signup, kept):
    """Toll-free verification wants the opt-in on the live form while sending stays off until approval."""
    from dataclasses import replace
    monkeypatch.setattr(web, "s", replace(web.s, sms_enabled=False, sms_signup=signup))
    c = TestClient(web.app)
    assert c.get("/api/config").json()["sms_signup"] is signup
    c.post("/api/subscribe", json=body())
    assert web.conn.execute("SELECT phone FROM subscriptions").fetchone()[0] == kept


def test_language_is_saved_for_alerts(web):
    TestClient(web.app).post("/api/subscribe", json=body(lang="zh"))
    assert web.conn.execute("SELECT lang FROM subscriptions").fetchone()[0] == "zh"


@pytest.mark.parametrize("override", [
    {"email": "", "phone": ""},                                                     # no contact
    {"carrier": "sprint"},                                                          # unknown carrier
    {"stores": []},                                                                 # nothing picked
    {"stores": [{"store": f"R00{i}", "store_name": "x", "parts": ["MK1E4LL/A"]} for i in range(13)]},   # > 12 stores
    {"stores": [{"store": "DROP", "store_name": "x", "parts": ["MK1E4LL/A"]}]},     # bad store id
    {"stores": [{"store": "R300", "store_name": "x", "parts": ["ZZZ"]}]},           # unknown model
])
def test_invalid_requests_rejected(web, override):
    assert TestClient(web.app).post("/api/subscribe", json=body(**override)).status_code == 400


def test_basic_auth_gates_site_but_not_webhook(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("BASIC_AUTH", "duo:secret")
    import importlib

    import duo_monitor.web as w
    c = TestClient(importlib.reload(w).app)
    assert c.get("/api/config").status_code == 401
    assert c.get("/api/config", auth=("duo", "wrong")).status_code == 401
    assert c.get("/api/config", auth=("duo", "secret")).status_code == 200
    assert c.post("/stripe/webhook", content=b"{}").status_code == 400      # reaches the handler, rejected by signature


def test_share_link_sets_cookie_and_skips_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ACCESS_KEY", "k3y")
    monkeypatch.delenv("BASIC_AUTH", raising=False)
    import importlib

    import duo_monitor.web as w
    c = TestClient(importlib.reload(w).app, base_url="https://testserver")
    assert c.get("/api/config").status_code == 401
    assert c.get("/?key=wrong", follow_redirects=False).status_code == 401
    r = c.get("/?key=k3y", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/api/config").status_code == 200          # cookie from the share link


def test_sms_queue_pull_is_token_protected_and_hands_out_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SMS_PULL_TOKEN", "tok")
    monkeypatch.setenv("ACCESS_KEY", "gate")          # queue endpoint must not sit behind the site gate
    import importlib

    import duo_monitor.web as w
    w = importlib.reload(w)
    from duo_monitor.notify import Notifier
    assert Notifier(w.s, w.conn).sms("+15015550123", "hi") == (True, "queued: sms queue")
    c = TestClient(w.app)
    assert c.get("/api/sms-queue").status_code == 401
    assert c.get("/api/sms-queue", headers={"X-Token": "tok"}).json() == [{"phone": "+15015550123", "text": "hi"}]
    assert c.get("/api/sms-queue", headers={"X-Token": "tok"}).json() == []


def test_admin_comp_and_expire(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")       # payments on: a normal sign-up would go to Stripe
    monkeypatch.setenv("ADMIN_TOKEN", "adm")
    import importlib

    import duo_monitor.web as w
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    c = TestClient(w.app)
    r = c.post("/api/subscribe", json=body(), headers={"X-Admin-Token": "adm"})
    assert r.json()["status"] == "active"
    sid = r.json()["id"]
    assert c.post(f"/api/admin/expire/{sid}").status_code == 401
    assert c.post(f"/api/admin/expire/{sid}", headers={"X-Admin-Token": "adm"}).json() == {"expired": True}
    assert w.db.watch_targets(w.conn) == {}


def test_gate_lets_signed_customer_links_and_public_pages_through(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ACCESS_KEY", "gate")
    import importlib

    import duo_monitor.web as w
    c = TestClient(importlib.reload(w).app)
    assert c.get("/").status_code == 401                                   # the form stays gated pre-launch
    for path in ("/unsub/x/bad", "/extend/x/bad", "/got/x/bad"):          # reach the handler: bad signature → 404, not 401
        assert c.get(path).status_code == 404, path
    assert c.get("/stories").status_code == 200
    assert c.get("/static/og.png").status_code == 200


def test_og_image_is_absolute(web):
    html = TestClient(web.app).get("/").text
    assert f'content="{web.s.base_url.rstrip("/")}/static/og.png"' in html and "__BASE__" not in html


def test_stopped_phone_never_gets_texts(web):
    from duo_monitor.notify import Notifier
    web.conn.execute("INSERT OR REPLACE INTO sms_consent (phone, status, at) VALUES ('+15015550123', 'stopped', 'x')")
    assert Notifier(web.s, web.conn).sms("+15015550123", "hi") == (False, "skipped: sms opted out (STOP)")


def test_sms_is_off_by_default_so_no_personal_number_is_used(monkeypatch):
    monkeypatch.setenv("SMS_ENABLED", "0")
    from duo_monitor.config import Settings
    from duo_monitor.notify import Notifier
    ok, detail = Notifier(Settings()).sms("+15015550123", "hi")
    assert not ok and detail.startswith("skipped: sms disabled")


def test_checkout_page_follows_the_chosen_language(tmp_path, monkeypatch):
    import importlib
    import stripe
    import duo_monitor.web as w
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    seen = []
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **kw: (seen.append(kw), type("S", (), {"id": "cs_1", "url": "https://checkout.test"})())[1])
    for lang in ("zh", "en"):
        assert TestClient(w.app).post("/api/subscribe", json=body(lang=lang)).json()["checkout_url"] == "https://checkout.test"
    assert [k["locale"] for k in seen] == ["zh", "en"]
