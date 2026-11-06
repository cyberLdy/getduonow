import importlib
from datetime import datetime, timedelta

import pytest

from duo_monitor import db


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import duo_monitor.web as web
    return importlib.reload(web)


def sub(w, expires_in, alerted=False):
    sid = db.create_subscription(w.conn, email="a@b.co", phone=None, carrier="unlocked",
                                 watches=[("R300", "Riverside", "P1", "iPhone Duo")])
    db.activate(w.conn, sid, days=14)
    w.conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(db.now() + expires_in), sid))
    if alerted:
        db.record_alert(w.conn, sub_id=sid, store="R300", part="P1", ok=True, detail="sent:", at=db.now())
    w.conn.commit()
    return sid


def test_unalerted_watch_is_extended_free_once(w, monkeypatch):
    from duo_monitor.features import guarantee
    sent = []
    monkeypatch.setattr(w.Notifier, "alert", lambda self, s, body, subject, **k: sent.append(subject) or (True, "x"))
    sid = sub(w, timedelta(minutes=5))
    guarantee._last_run = 0
    guarantee.run(w.conn)
    exp = datetime.fromisoformat(db.get_subscription(w.conn, sid)["expires_at"])
    assert exp > db.now() + timedelta(days=13, hours=23) and len(sent) == 1
    guarantee._last_run = 0
    w.conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(db.now() + timedelta(minutes=5)), sid))
    guarantee.run(w.conn)
    assert len(sent) == 1                      # only one free extension per pass


def test_alerted_or_far_from_expiry_watches_are_left_alone(w, monkeypatch):
    from duo_monitor.features import guarantee
    monkeypatch.setattr(w.Notifier, "alert", lambda *a, **k: (True, "x"))
    a = sub(w, timedelta(minutes=5), alerted=True)
    b = sub(w, timedelta(days=3))
    guarantee._last_run = 0
    guarantee.run(w.conn)
    assert w.conn.execute("SELECT COUNT(*) FROM guarantee").fetchone()[0] == 0
    assert {a, b}
