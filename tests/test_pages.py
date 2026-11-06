import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    import duo_monitor.web as w
    w = importlib.reload(w)
    return TestClient(w.app)


def test_terms_page_in_each_language(client):
    en, zh = client.get("/terms").text, client.get("/terms?lang=zh").text
    for s in ["about every 30 seconds", "You order from Apple yourself", "Not affiliated with Apple",
              "One-time payment", "{price}", "we automatically extend it once for free",
              "Message frequency varies with restocks", "Msg & data rates may apply", "Reply STOP to opt out, HELP for help",
              "not a condition of purchase", 'id="sms"', 'id="privacy"', "/api/config"]:
        assert s in en, s
    for s in ["大约每 30 秒", "下单由你自己在 Apple 完成", "与 Apple Inc. 无关", "一次性付费",
              "延长期结束仍然没有到货，7 天内回复任意一封 GetDuoNow 邮件即可全额退款", "回复 STOP 退订", "不是购买的前提条件",
              'lang="zh-Hans"']:
        assert s in zh, s
    assert 'dir="rtl"' in client.get("/terms?lang=ar").text and 'dir="ltr"' in en


def test_done_page_in_each_language_with_share_block_and_log(client):
    en, zh = client.get("/done?sub=abc123").text, client.get("/done?sub=abc123&lang=zh").text
    for s in ["You're on the list", "What happens next", "within seconds", "sell out in minutes",
              "Paid watches start once your payment confirms", "Share with friends", "/api/referral/link?sub=",
              "navigator.share", "clipboard", "/api/watch/", "Live log", 'href="/"']:
        assert s in en, s
    for s in ["已开始帮你盯", "接下来", "几秒内", "马上在 Apple 下单", "付款确认后开始", "分享给朋友", "实时日志"]:
        assert s in zh, s
    assert 'id="share" hidden' in en and 'id="log" hidden' in en   # both stay hidden until their API answers


def test_language_choice_is_remembered_and_browser_language_is_the_default(client):
    r = client.get("/?lang=es")
    assert r.cookies.get("gdn_lang") == "es" and '"lang": "es"' in r.text
    assert '"lang": "es"' in client.get("/", headers={"accept-language": "ko"}).text    # the saved choice beats the browser
    client.cookies.clear()
    assert '"lang": "ko"' in client.get("/", headers={"accept-language": "ko-KR,ko;q=0.9,en;q=0.8"}).text
    assert '"lang": "zh-Hant"' in client.get("/", headers={"accept-language": "zh-TW"}).text
    assert '"lang": "en"' in client.get("/", headers={"accept-language": "de-DE"}).text


def test_config_exposes_what_terms_reads(client):
    cfg = client.get("/api/config").json()
    assert cfg["price_cents"] > 0 and cfg["watch_days"] > 0 and "payments" in cfg


def test_short_language_links(client):
    for code in ("zh", "zh-Hant", "es", "ar"):
        r = client.get(f"/{code}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"/?lang={code}"
    assert client.get("/stories").status_code == 200 and client.get("/xx").status_code == 404
