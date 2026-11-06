import importlib

import pytest
from fastapi.testclient import TestClient

from duo_monitor import poller


@pytest.fixture
def load_web(tmp_path, monkeypatch):
    def load(run_poller: bool):
        monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setenv("RUN_POLLER", "1" if run_poller else "0")
        import duo_monitor.web as w
        return importlib.reload(w)
    return load


def test_healthz_reports_last_cycle_age(load_web, monkeypatch):
    w = load_web(run_poller=True)
    monkeypatch.setattr(poller, "last_pass_at", None)
    r = TestClient(w.app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "last_cycle_age": None, "apple": {"requests": 0, "errors": 0, "exits": 0, "resting": 0}}     # startup grace
    monkeypatch.setattr(poller, "last_pass_at", w.time.time() - 42)
    r = TestClient(w.app).get("/healthz")
    assert r.status_code == 200 and 41 <= r.json()["last_cycle_age"] <= 43


def test_healthz_503_when_poller_is_stale(load_web, monkeypatch):
    w = load_web(run_poller=True)
    monkeypatch.setattr(poller, "last_pass_at", w.time.time() - w.POLLER_STALE - 5)
    r = TestClient(w.app).get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False


def test_healthz_503_when_no_pass_ever_completed_after_grace(load_web, monkeypatch):
    w = load_web(run_poller=True)
    monkeypatch.setattr(poller, "last_pass_at", None)
    monkeypatch.setattr(w, "STARTED_AT", w.time.time() - w.POLLER_STALE - 5)
    assert TestClient(w.app).get("/healthz").status_code == 503


def test_healthz_never_503_without_the_poller(load_web, monkeypatch):
    w = load_web(run_poller=False)
    monkeypatch.setattr(poller, "last_pass_at", None)
    monkeypatch.setattr(w, "STARTED_AT", w.time.time() - 10_000)
    r = TestClient(w.app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "last_cycle_age": None, "apple": {"requests": 0, "errors": 0, "exits": 0, "resting": 0}}
