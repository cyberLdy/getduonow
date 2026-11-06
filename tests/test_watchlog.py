"""Step ⑤ live log: a signed link shows what we check, each store × model's state, and what happened."""
import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

WATCH = [("R300", "Riverside", "MK1E4LL/A", "iPhone Duo 256GB Star White"),
         ("R300", "Riverside", "MK1G4LL/A", "iPhone Duo 512GB Star White")]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    import duo_monitor.web as w
    return importlib.reload(w)


def test_log_needs_the_signed_link(web):
    sid = web.db.create_subscription(web.conn, email="a@example.com", phone=None, carrier="unlocked", watches=WATCH)
    c = TestClient(web.app)
    assert c.get(f"/api/watch/{sid}/bad").status_code == 404
    assert c.get(f"/api/watch/{sid}/{web.signing.sign(sid, 'unsub')}").status_code == 404    # other purposes don't open it
    assert c.get(f"/api/watch/{sid}/{web.signing.sign(sid, 'watch')}").json()["status"] == "pending"


def test_log_shows_states_alerts_and_restocks(web):
    db, conn, now = web.db, web.conn, web.db.now()
    sid = db.create_subscription(conn, email="a@example.com", phone=None, carrier="unlocked", watches=WATCH)
    db.activate(conn, sid, 14)
    db.upsert_availability(conn, store="R300", part="MK1E4LL/A", available=True, store_name="Riverside", quote="Today", at=now)
    db.upsert_availability(conn, store="R300", part="MK1G4LL/A", available=False, store_name="Riverside", quote="", at=now)
    db.record_alert(conn, sub_id=sid, store="R300", part="MK1E4LL/A", ok=True, detail="sent: sms textbelt", at=now)
    web.hooks.emit("cell.soldout", conn, store="R300", part="MK1G4LL/A", store_name="Riverside",
                   since=db.iso(now + timedelta(minutes=1)), minutes=28)
    web.hooks.emit("cell.soldout", conn, store="R030", part="MK1G4LL/A", store_name="Lakeside",   # not watched
                   since=db.iso(now + timedelta(minutes=1)), minutes=5)
    log = TestClient(web.app).get(f"/api/watch/{sid}/{web.signing.sign(sid, 'watch')}").json()
    assert log["status"] == "active" and log["expires_at"]
    assert {(c["product"], c["available"]) for c in log["cells"]} == {
        ("iPhone Duo 256GB Star White", True), ("iPhone Duo 512GB Star White", False)}
    kinds = [(e["kind"], e.get("product"), e.get("minutes")) for e in log["events"]]
    assert ("alert", "iPhone Duo 256GB Star White", None) in kinds
    assert ("restock", "iPhone Duo 512GB Star White", 28) in kinds and ("start", None, None) in kinds
    assert all(e.get("store_name") != "Lakeside" for e in log["events"])
    assert "email" not in str(log) and "phone" not in str(log)               # no personal data in the log


def test_welcome_email_links_to_the_live_log_but_the_text_does_not(web):
    from duo_monitor import hooks
    sid = web.db.create_subscription(web.conn, email="a@example.com", phone="+15015550123", carrier="unlocked", watches=WATCH)
    sub = web.db.get_subscription(web.conn, sid)
    link = f"/done?sub={sid}&sig={web.signing.sign(sid, 'watch')}"
    assert link in hooks.footer_text(sub, "welcome", "en", "email")
    assert link not in hooks.footer_text(sub, "welcome", "en", "sms") and link not in hooks.footer_text(sub, "alert", "en", "email")


def test_recent_restocks_are_public_and_carry_no_personal_data(web):
    from fastapi.testclient import TestClient
    c = TestClient(web.app)
    assert c.get("/api/restocks/recent").json() == {"count": 0, "since": None, "items": [], "live": [], "last_check_s": None}
    web.db.upsert_availability(web.conn, store="R030", part="MK1E4LL/A", available=True, store_name="Lakeside",
                               quote="Today", at=web.db.now())
    assert c.get("/api/restocks/recent").json()["live"] == []        # nobody watches that cell: its state is not refreshed
    sid = web.db.create_subscription(web.conn, email="a@example.com", phone=None, carrier="unlocked",
                                     watches=[("R030", "Lakeside", "MK1E4LL/A", "iPhone Duo 256GB Star White")])
    web.db.activate(web.conn, sid, 14)
    assert c.get("/api/restocks/recent").json()["live"][0]["part"] == "MK1E4LL/A"               # watched + in stock
    web.hooks.emit("cell.soldout", web.conn, store="R300", part="MK1G4LL/A", store_name="Riverside Mall",
                   since="2026-11-03T15:52:10+00:00", minutes=1)
    web.hooks.emit("cell.soldout", web.conn, store="R030", part="MK1G4LL/A", store_name="Lakeside",
                   since="2026-10-31T14:55:40+00:00", minutes=2)
    r = c.get("/api/restocks/recent").json()
    assert r["count"] == 2 and r["since"].startswith("2026-10-31") and r["items"][0]["store_name"].startswith("Apple •")
    assert "Riverside" not in str(r) and "Lakeside" not in str(r)                 # public proof never names a store
    assert set(r["items"][0]) == {"store_name", "part", "started_at", "minutes"}


def test_admin_imports_historic_restocks_once(web, monkeypatch):
    from fastapi.testclient import TestClient
    c = TestClient(web.app)
    rows = [{"store": "R300", "part": "MK1E4LL/A", "store_name": "Riverside Mall",
             "started_at": "2026-11-01T17:57:21+00:00", "minutes": 67}]
    assert c.post("/admin/restocks/import", json=rows).status_code in (401, 404)          # admin only
    from dataclasses import replace
    monkeypatch.setattr(web, "s", replace(web.s, admin_token="adm"))
    assert c.post("/admin/restocks/import", json=rows, headers={"X-Admin-Token": "adm"}).json() == {"imported": 1}
    assert c.post("/admin/restocks/import", json=rows, headers={"X-Admin-Token": "adm"}).json() == {"imported": 0}   # idempotent
    assert c.get("/api/restocks/recent").json()["items"][0]["minutes"] == 67
