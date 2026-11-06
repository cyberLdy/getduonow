import struct

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import importlib

    import duo_monitor.web as w
    w = importlib.reload(w)
    return TestClient(w.app)


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_og_png(client):
    r = client.get("/static/og.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert png_size(r.content) == (1200, 630)


def test_favicon_svg(client):
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith("<svg")


def test_apple_touch_icon(client):
    r = client.get("/static/apple-touch-icon.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert png_size(r.content) == (180, 180)
