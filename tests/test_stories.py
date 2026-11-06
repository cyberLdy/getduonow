import importlib

import pytest
from fastapi.testclient import TestClient

WATCHES = [("R300", "Riverside Mall", "MK1E4LL/A", "iPhone Duo 256GB Star White"),
           ("R030", "Lakeside", "MK1F4LL/A", "iPhone Duo 256GB Night Sky")]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    for k in ("STRIPE_SECRET_KEY", "BASIC_AUTH", "ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    import duo_monitor.web as w
    return importlib.reload(w)


def _sub(w, lang="en"):
    sub_id = w.db.create_subscription(w.conn, email="me@example.com", phone=None, carrier="unlocked",
                                      watches=WATCHES, lang=lang)
    return sub_id, w.db.get_subscription(w.conn, sub_id)


def _url(sub_id):
    from duo_monitor import signing
    return signing.link("", "got", sub_id)


def _post(w, sub_id, **form):
    return TestClient(w.app).post(_url(sub_id), data=form)


def test_footer_only_on_alert_kind(web):
    from duo_monitor import hooks
    sub_id, sub = _sub(web)
    link = _url(sub_id)
    assert f"Got one? Tell us: {web.s.base_url}{link}" in hooks.footer_text(sub, "alert", "en")
    assert f"抢到了吗？告诉我们：{web.s.base_url}{link}" in hooks.footer_text(sub, "alert", "zh")
    assert "/got/" not in hooks.footer_text(sub, "welcome", "en")


def test_bad_sig_is_404(web):
    sub_id, _ = _sub(web)
    c = TestClient(web.app)
    assert c.get(f"/got/{sub_id}/deadbeef").status_code == 404
    assert c.post(f"/got/{sub_id}/deadbeef", data={"got": "1"}).status_code == 404
    assert c.get(_url("nosuchsub")).status_code == 404   # valid signature, unknown subscription
    assert web.conn.execute("SELECT COUNT(*) FROM success_stories").fetchone()[0] == 0


def test_form_page_is_bilingual(web):
    sub_id, _ = _sub(web)
    c = TestClient(web.app)
    en = c.get(_url(sub_id)).text
    assert "Did you get it?" in en and "Not yet" in en
    assert 'name="consent" value="1">' in en and "checked" not in en.split('name="consent"')[1][:20]
    assert "抢到了吗？" in c.get(_url(sub_id) + "?lang=zh").text
    zh_id, _ = _sub(web, lang="zh")                 # the watch's own language beats the browser's
    assert "抢到了吗？" in c.get(_url(zh_id), headers={"accept-language": "en-US"}).text
    assert 'dir="rtl"' in c.get(_url(sub_id) + "?lang=fa").text


def test_save_uses_latest_alert_and_resubmission_updates(web):
    sub_id, _ = _sub(web)
    web.db.record_alert(web.conn, sub_id=sub_id, store="R030", part="MK1F4LL/A", ok=True, detail="", at=web.db.now())
    r = _post(web, sub_id, got="1", name="Dana", city="Springfield", quote="x" * 300, consent="1")
    assert r.status_code == 200 and "Congrats" in r.text
    row = web.conn.execute("SELECT * FROM success_stories WHERE sub_id=?", (sub_id,)).fetchone()
    assert (row["got"], row["consent"], row["product"], row["store_name"]) == (1, 1, "iPhone Duo 256GB Night Sky", "Lakeside")
    assert len(row["quote"]) == 140
    _post(web, sub_id, got="1", name="Dana", city="Springfield")    # resubmitted without consent
    rows = web.conn.execute("SELECT consent FROM success_stories WHERE sub_id=?", (sub_id,)).fetchall()
    assert [r["consent"] for r in rows] == [0]


def test_product_falls_back_to_first_watch(web):
    sub_id, _ = _sub(web)
    _post(web, sub_id, got="0")
    row = web.conn.execute("SELECT got, product, store_name FROM success_stories").fetchone()
    assert tuple(row) == (0, "iPhone Duo 256GB Night Sky", "Lakeside")   # get_watches orders by store: R030 first


def test_only_consented_stories_are_public(web):
    a, _ = _sub(web)
    b, _ = _sub(web)
    c, _ = _sub(web)
    _post(web, a, got="1", name="Dana", city="Springfield", quote="Got it in 4 minutes", consent="1")
    _post(web, b, got="1", name="Secretname", city="Hiddenville", quote="private words")   # no consent
    _post(web, c, got="0", name="Notyet", consent="1")                                    # not yet: never a story
    page = TestClient(web.app).get("/stories").text
    assert "D., Springfield" in page and "Got it in 4 minutes" in page and "Dana" not in page
    assert "2 customers told us" in page
    for secret in ("Secretname", "S.,", "Hiddenville", "private words", "Notyet", "N.,"):
        assert secret not in page
    summary = TestClient(web.app).get("/api/stories/summary").json()
    assert summary["got"] == 2 and [s["initial"] for s in summary["stories"]] == ["D."]
    assert "sub_id" not in summary["stories"][0] and "name" not in summary["stories"][0]


def test_empty_state(web, tmp_path, monkeypatch):
    from duo_monitor.features import stories
    monkeypatch.setattr(stories, "PROOF", tmp_path / "none.json")    # independent of real screenshots in static/proof
    page = TestClient(web.app).get("/stories").text
    assert "No stories yet" in page
    assert "还没有公开的故事" in TestClient(web.app).get("/stories?lang=zh").text
    assert TestClient(web.app).get("/api/stories/summary").json() == {"got": 0, "stories": [], "proof": 0}


def test_user_input_is_escaped(web, tmp_path, monkeypatch):
    from duo_monitor.features import stories
    monkeypatch.setattr(stories, "PROOF", tmp_path / "none.json")    # our own <img> screenshots are not user input
    sub_id, _ = _sub(web)
    _post(web, sub_id, got="1", name="<b>eve</b>", city='Tom & "Jerry\'s"',
          quote="<script>alert(1)</script> & <img src=x onerror=alert(1)>", consent="on")
    page = TestClient(web.app).get("/stories").text
    assert "<script>alert" not in page and "<img" not in page and "<b>" not in page
    assert "Tom &amp; &quot;Jerry&#x27;s&quot;" in page
    assert "&amp; " in page.split("<blockquote>")[1]


def test_story_date_is_us_day_not_utc(web):
    sub_id, _ = _sub(web)
    _post(web, sub_id, got="1", name="Dana", consent="1")
    web.conn.execute("UPDATE success_stories SET created_at='2026-11-02T01:30:00.000000+00:00'")   # Nov 1, evening in the US
    assert TestClient(web.app).get("/api/stories/summary").json()["stories"][0]["date"] == "2026-11-01"
    assert "Nov 1, 2026" in TestClient(web.app).get("/stories").text


def test_real_order_screenshots_show_on_the_stories_page(web, tmp_path, monkeypatch):
    import json
    from duo_monitor.features import stories
    c = TestClient(web.app)
    monkeypatch.setattr(stories, "PROOF", tmp_path / "missing.json")
    assert "Real orders" not in c.get("/stories").text and c.get("/api/stories/summary").json()["proof"] == 0
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps([{"src": "/static/proof/a.webp", "en": "Picked up in Springfield", "zh": "斯普林菲尔德到店取机"}]))
    monkeypatch.setattr(stories, "PROOF", proof)
    page = c.get("/stories").text
    assert "Real orders" in page and 'src="/static/proof/a.webp"' in page and "Picked up in Springfield" in page
    assert "No stories yet" not in page                       # screenshots are the proof; no empty-state line
    assert "斯普林菲尔德到店取机" in c.get("/stories?lang=zh").text and c.get("/api/stories/summary").json()["proof"] == 1
