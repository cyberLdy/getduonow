import importlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

CATALOG = [{"part": "MK1E4LL/A", "name": "iPhone Duo 256GB Star White", "size": "256GB", "color": "Star White", "price": 1999.0}]
H = {"X-Admin-Token": "adm"}


def body(email="me@example.com"):
    return {"email": email, "phone": "501 555 0123", "sms_opt_in": True, "carrier": "unlocked", "lang": "zh",
            "stores": [{"store": "R300", "store_name": "Riverside Mall", "parts": ["MK1E4LL/A"]}]}


def _reload(tmp_path, monkeypatch, token="adm"):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("PRICE_CENTS", "500")
    for k in ("STRIPE_SECRET_KEY", "ACCESS_KEY", "BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    if token:
        monkeypatch.setenv("ADMIN_TOKEN", token)
    else:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    import duo_monitor.web as w
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    monkeypatch.setattr(w, "_welcome", lambda sub_id: None)      # never send a real welcome
    return w


@pytest.fixture
def web(tmp_path, monkeypatch):
    return _reload(tmp_path, monkeypatch)


def events(w, sub_id=None):
    sql, args = ("SELECT * FROM events WHERE sub_id=? ORDER BY id", (sub_id,)) if sub_id else ("SELECT * FROM events ORDER BY id", ())
    return [(r["event"], r["sub_id"], json.loads(r["data"])) for r in w.conn.execute(sql, args)]


def test_admin_404_when_no_token_configured(tmp_path, monkeypatch):
    c = TestClient(_reload(tmp_path, monkeypatch, token="").app)
    for path in ("/admin", "/admin?token=x", "/admin/user?email=a@b.co", "/admin/export.csv"):
        assert c.get(path, headers={"X-Admin-Token": ""}).status_code == 404


def test_admin_401_without_valid_token(web):
    c = TestClient(web.app)
    for path in ("/admin", "/admin/user?email=a@b.co", "/admin/export.csv"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-Admin-Token": "nope"}).status_code == 401
        assert TestClient(web.app, cookies={"gdn_admin": "nope"}).get(path).status_code == 401
        assert c.get(path, headers=H).status_code == 200
    assert c.get("/admin?token=nope", follow_redirects=False).status_code == 401


def test_token_link_sets_cookie_then_pages_open(web):
    c = TestClient(web.app, base_url="https://testserver")
    r = c.get("/admin?token=adm", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert "gdn_admin=adm" in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/admin").status_code == 200
    assert c.get("/admin/user?email=me@example.com").status_code == 200
    assert c.get("/admin/export.csv").status_code == 200


def test_events_recorded_on_free_subscribe(web):
    sid = TestClient(web.app).post("/api/subscribe", json=body(), headers={"X-Admin-Token": ""}).json()["id"]
    assert events(web, sid) == [("subscription.created", sid, {"ref": ""}),
                                ("subscription.activated", sid, {"via": "free"})]


def test_events_without_subscription_have_null_sub_id(web):
    web.hooks.emit("subscriptions.expired", web.conn, count=2)
    web.hooks.emit("cell.soldout", web.conn, store="R300", part="MK1E4LL/A", store_name="Riverside", since="x", minutes=3.5)
    web.hooks.emit("alert.sent", web.conn, sub_id="abc", store="R300", part="MK1E4LL/A", ok=False, detail="sms: bounce")
    web.hooks.emit("poll.pass", web.conn, stats={})      # not logged: fires every 30 s
    assert events(web) == [
        ("subscriptions.expired", None, {"count": 2}),
        ("cell.soldout", None, {"store": "R300", "part": "MK1E4LL/A", "store_name": "Riverside", "since": "x", "minutes": 3.5}),
        ("alert.sent", "abc", {"store": "R300", "part": "MK1E4LL/A", "ok": False, "detail": "sms: bounce"})]


def _seed(w):
    """free-active + paid-active + pending + expired; 2 sent alerts (now, 3 days ago) + 1 failed."""
    c = TestClient(w.app)
    free = c.post("/api/subscribe", json=body()).json()["id"]
    watch = [("R300", "Riverside Mall", "MK1E4LL/A", "iPhone Duo 256GB Star White")]
    paid = w.db.create_subscription(w.conn, email="Pay@Example.com", phone=None, carrier="att", watches=watch)
    w.db.activate(w.conn, paid, 14, checkout_session="cs_test_1")
    w.db.create_subscription(w.conn, email="pay@example.com", phone=None, carrier="att", watches=watch)   # pending
    gone = w.db.create_subscription(w.conn, email="<b>x@evil.io", phone=None, carrier="att", watches=watch)
    w.db.expire(w.conn, gone)
    now = w.db.now()
    w.db.record_alert(w.conn, sub_id=free, store="R300", part="MK1E4LL/A", ok=True, detail="sms ok; email ok", at=now)
    w.db.record_alert(w.conn, sub_id=free, store="R300", part="MK1E4LL/A", ok=True, detail="old", at=now - timedelta(days=3))
    w.db.record_alert(w.conn, sub_id=paid, store="R300", part="MK1E4LL/A", ok=False, detail="sms: carrier rejected", at=now)
    return free, paid


def test_overview_numbers(web):
    _seed(web)
    from duo_monitor.features import admin
    assert admin.stats(web.conn, web.s.price_cents) == {
        "active": 2, "pending": 1, "expired": 1, "paid": 1, "revenue_cents": 500,
        "sent_24h": 1, "sent_7d": 2, "failed_7d": 1, "customers": 3}
    html = TestClient(web.app).get("/admin?lang=en", headers=H).text
    assert "$5.00" in html and "Revenue" in html and "…0123" in html and "Riverside Mall" in html
    assert "subscription.activated" in html and "<b>x" not in html          # latest events listed; user text escaped
    assert "收入" in TestClient(web.app).get("/admin", headers={**H, "Accept-Language": "zh-CN,zh"}).text


def test_user_page_shows_alerts_and_timeline(web):
    _seed(web)
    html = TestClient(web.app).get("/admin/user?email=PAY@example.com&lang=en", headers=H).text
    assert "sms: carrier rejected" in html and "iPhone Duo 256GB Star White" in html and "failed" in html
    assert "cs_test" not in html and "sms ok; email ok" not in html            # other customers' alerts stay out
    me = TestClient(web.app).get("/admin/user?email=me@example.com", headers=H).text
    assert "sms ok; email ok" in me and "subscription.created" in me and "+15015550123" in me


def test_csv_header_and_rows(web):
    _seed(web)
    r = TestClient(web.app).get("/admin/export.csv", headers=H)
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0] == ("id,created_at,status,email,phone,carrier,lang,expires_at,checkout_session,"
                        "n_watches,stores,n_alerts,n_failed")
    assert len(lines) == 5 and any("'+15015550123" in ln for ln in lines)
