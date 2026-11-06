"""Abuse limits: store lookups reach Apple from the same IP the poller uses, so a flood must never get through."""
import importlib
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

CATALOG = [{"family": "iphone-duo", "part": "MK1E4LL/A", "name": "iPhone Duo 256GB Star White", "size": "256GB",
            "color": "Star White", "price": 1999.0}]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    import duo_monitor.web as w
    w = importlib.reload(w)
    monkeypatch.setattr(w, "catalog", lambda: CATALOG)
    w.apple_calls = []

    def near(location, parts):
        w.apple_calls.append(location)
        return [NS(number="R300", name="Riverside", city="Springfield", state="AR", miles=3.1)], []
    monkeypatch.setattr(w.client, "availability_near", near)
    return w


def lookup(c, loc, ip="203.0.113.1"):
    return c.get("/api/stores", params={"location": loc}, headers={"cf-connecting-ip": ip})


def test_repeat_lookup_is_served_from_cache(web):
    c = TestClient(web.app)
    assert lookup(c, "10001").status_code == 200 and lookup(c, " 10001 ").status_code == 200
    assert web.apple_calls == ["10001"]


def test_one_visitor_cannot_flood_store_lookups(web):
    c = TestClient(web.app)
    codes = [lookup(c, str(70000 + i)).status_code for i in range(25)]
    assert codes[:20] == [200] * 20 and set(codes[20:]) == {429}
    assert lookup(c, "10001", ip="203.0.113.2").status_code == 200        # other visitors unaffected


def test_global_budget_caps_apple_calls_from_many_visitors(web, monkeypatch):
    monkeypatch.setattr(web, "APPLE_BUDGET", web.RateLimit(5, 60))
    c = TestClient(web.app)
    codes = [lookup(c, str(70000 + i), ip=f"198.51.100.{i}").status_code for i in range(8)]
    assert codes == [200] * 5 + [429] * 3 and len(web.apple_calls) == 5
    assert lookup(c, "70000", ip="198.51.100.99").status_code == 200       # cached answers still flow


def test_one_visitor_cannot_flood_signups(web):
    c = TestClient(web.app)
    body = {"email": "a@example.com", "stores": [{"store": "R300", "store_name": "Riverside", "parts": ["MK1E4LL/A"]}]}
    codes = [c.post("/api/subscribe", json=body, headers={"cf-connecting-ip": "203.0.113.7"}).status_code
             for _ in range(12)]
    assert codes[:10] == [200] * 10 and set(codes[10:]) == {429}


def test_client_ip_prefers_the_edge_header():
    from duo_monitor.ratelimit import client_ip
    req = NS(headers={"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "6.6.6.6, 203.0.113.9"},
             client=NS(host="10.0.0.1"))
    assert client_ip(req) == "203.0.113.9"
    assert client_ip(NS(headers={"x-forwarded-for": "203.0.113.4, 10.1.1.1"}, client=NS(host="10.0.0.1"))) == "203.0.113.4"
    assert client_ip(NS(headers={}, client=NS(host="10.0.0.1"))) == "10.0.0.1"
