import json
import sqlite3
import threading
import time
from datetime import timedelta

import httpx
import pytest

from duo_monitor import db, hooks, poller
from duo_monitor.apple import AppleClient, Cell, parse_catalog, parse_pickup, parse_stores
from duo_monitor.poller import chunks, poll_once


def store_payload(store="R300", name="Riverside Mall", parts=None):
    parts = parts or {"MK1E4LL/A": True}
    return {"body": {"stores": [{
        "storeNumber": store, "storeName": name, "city": "Springfield", "state": "AR", "storedistance": 3.3,
        "partsAvailability": {p: {
            "pickupDisplay": "available" if ok else "unavailable",
            "pickupSearchQuote": "Available Today" if ok else "Currently unavailable",
            "messageTypes": {"compact": {"storePickupProductTitle": f"Phone {p}"}}} for p, ok in parts.items()}}]}}


def test_parse_pickup_reads_availability_per_part():
    cells = parse_pickup(store_payload(parts={"A": True, "B": False}))
    assert [(c.part, c.available, c.quote) for c in cells] == [
        ("A", True, "Available Today"), ("B", False, "Currently unavailable")]
    assert cells[0].store == "R300" and cells[0].product == "Phone A"


def test_parse_stores():
    assert parse_stores(store_payload())[0].number == "R300"


def test_parse_catalog_dedupes_parts():
    products = [{"partNumber": "MK1E4LL/A", "name": "Duo 256GB Star White", "price": {"fullPrice": 1999.0}},
                {"partNumber": "MK1E4LL/A", "name": "dup"}]
    html = f'<script type="application/json" id="metrics">{json.dumps({"data": {"products": products}})}</script>'
    got = parse_catalog(html)
    assert [(p.part, p.price) for p in got] == [("MK1E4LL/A", 1999.0)]


def test_more_than_eight_parts_is_rejected():
    with pytest.raises(ValueError):
        AppleClient()._pickup([f"P{i}" for i in range(9)], store="R300")


def test_chunks_of_eight():
    assert [len(c) for c in chunks([str(i) for i in range(17)])] == [8, 8, 1]


class FakeClient:
    def __init__(self):
        self.stock: dict[tuple[str, str], bool] = {}
        self.calls: list[tuple[str, list[str]]] = []

    def availability(self, store, parts):
        self.calls.append((store, parts))
        return [Cell(store, "Store " + store, "City", p, "Phone " + p, self.stock.get((store, p), False),
                     "Available Today" if self.stock.get((store, p)) else "Currently unavailable") for p in parts]


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def alert(self, sub, text, subject, kind=None, sms=None):
        self.sent.append((sub["phone"], subject, text))
        return True, "fake"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def active_sub(conn, contact="+15015550123", part="MK1E4LL/A", stores=("R300",), carrier="unlocked"):
    sid = db.create_subscription(conn, email=None, phone=contact, carrier=carrier,
                                 watches=[(st, "Store " + st, part, "iPhone Duo 256GB Star White") for st in stores])
    db.activate(conn, sid, days=14)
    db.confirm_sms(conn, contact)       # replied YES to the confirmation text
    return sid


def test_alert_once_per_restock_and_again_after_next_restock(conn):
    active_sub(conn)
    c, n = FakeClient(), FakeNotifier()
    poll_once(conn, c, n)
    assert n.sent == []                                    # out of stock
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    poll_once(conn, c, n)
    assert len(n.sent) == 1                                # in stock: one alert, not repeated
    c.stock[("R300", "MK1E4LL/A")] = False
    poll_once(conn, c, n)
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    assert len(n.sent) == 2                                # restocked again: alerted again


def test_new_subscriber_to_cell_already_in_stock_is_alerted(conn):
    active_sub(conn, contact="+15015550001")
    c, n = FakeClient(), FakeNotifier()
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    active_sub(conn, contact="+15015550002")
    poll_once(conn, c, n)
    assert [s[0] for s in n.sent] == ["+15015550001", "+15015550002"]


def test_only_watched_stores_and_parts_are_polled_and_batched(conn):
    for i in range(10):
        active_sub(conn, part=f"P{i}", stores=("R300", "R030"))
    c = FakeClient()
    poll_once(conn, c, FakeNotifier())
    assert sorted((s, len(p)) for s, p in c.calls) == [("R030", 2), ("R030", 8), ("R300", 2), ("R300", 8)]


def test_pending_and_expired_subscriptions_are_not_polled(conn):
    db.create_subscription(conn, email=None, phone="+1", carrier="unlocked", watches=[("R300", "S", "X", "P")])   # pending
    sid = active_sub(conn, part="Y")
    conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (db.iso(db.now() - timedelta(minutes=1)), sid))
    c = FakeClient()
    stats = poll_once(conn, c, FakeNotifier())
    assert c.calls == [] and stats["expired"] == 1


def test_subscriber_watching_other_store_is_not_alerted(conn):
    active_sub(conn, stores=("R030",))
    c, n = FakeClient(), FakeNotifier()
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    assert n.sent == []


def test_per_store_model_choices_are_respected(conn):
    # white at both stores, blue only at R300 (the shape of a real request)
    sid = db.create_subscription(conn, email="a@b.co", phone="+15015550123", carrier="att", watches=[
        ("R300", "Riverside", "WHITE", "Duo White"), ("R300", "Riverside", "BLUE", "Duo Blue"), ("R030", "Lakeside", "WHITE", "Duo White")])
    db.activate(conn, sid, days=14)
    assert db.watch_targets(conn) == {"R300": {"WHITE", "BLUE"}, "R030": {"WHITE"}}
    c, n = FakeClient(), FakeNotifier()
    c.stock[("R030", "BLUE")] = True          # not watched at R030
    c.stock[("R030", "WHITE")] = True
    poll_once(conn, c, n)
    assert len(n.sent) == 1 and "Duo White" in n.sent[0][1] and "(AT&T)" in n.sent[0][2] and "STOP" in n.sent[0][2]


def test_chinese_alert_copy(conn):
    sid = db.create_subscription(conn, email="a@b.co", phone=None, carrier="unlocked", lang="zh",
                                 watches=[("R300", "Riverside", "WHITE", "iPhone Duo 256GB Star White")])
    db.activate(conn, sid, days=14)
    c, n = FakeClient(), FakeNotifier()
    c.stock[("R300", "WHITE")] = True
    poll_once(conn, c, n)
    assert "有货" in n.sent[0][1] and "无锁版" in n.sent[0][2] and "/shop/product/WHITE" in n.sent[0][2]


# ---- poller speed: pooled client, concurrent fetch, commit before sending -----------------------------------------

def test_apple_client_reuses_one_pooled_client_per_proxy():
    c = AppleClient()
    assert c._client() is c._client()
    p = AppleClient(["http://p1:1", "http://p2:1"])
    a, b, a2 = p._client(), p._client(), p._client()
    assert a is not b and a is a2                          # proxies still rotate, each keeps its own pool


def test_zip_with_no_apple_store_nearby_is_an_empty_list_not_an_error():
    """Montana has no Apple Store: Apple answers without a "stores" list (seen for 59101 → 502 before)."""
    c = AppleClient()
    c._pickup = lambda parts, **where: {"head": {"status": "200"}, "body": {}}
    assert c.availability_near("59101", ["MK1E4LL/A"]) == ([], [])
    assert parse_stores({"head": {}, "body": {}}) == []


def test_apple_request_retries_once_through_the_next_proxy():
    """One bad exit (blocked / dead) must not cost a 30-second check: retry once via the next proxy."""
    calls = []

    class Exit:
        def __init__(self, status):
            self.status = status

        def get(self, url, params=None):
            calls.append(self.status)
            return httpx.Response(self.status, json={"ok": True}, request=httpx.Request("GET", url))
    c = AppleClient(["http://bad:1", "http://good:1"])
    c._clients = {"http://bad:1": Exit(541), "http://good:1": Exit(200)}
    assert c._get("/x").json() == {"ok": True} and calls == [541, 200]
    both_bad = AppleClient(["http://bad:1", "http://bad2:1"])
    both_bad._clients = {"http://bad:1": Exit(541), "http://bad2:1": Exit(541)}
    with pytest.raises(httpx.HTTPStatusError):
        both_bad._get("/x")


class StatusExit:
    """A pooled client stand-in that answers one HTTP status and counts its requests."""
    def __init__(self, status):
        self.status, self.n = status, 0

    def get(self, url, params=None):
        self.n += 1
        return httpx.Response(self.status, json={"ok": True}, request=httpx.Request("GET", url))


def test_refused_exit_rests_and_is_skipped_until_it_wakes(monkeypatch):
    """11-04/05: Apple refuses an IP that asks too often for ~30 min. Hitting it again (and retrying onto the next exit
    every time) spread the refusal across all 9 exits. A refused exit rests; the rotation skips it until it wakes."""
    clock = [1000.0]
    monkeypatch.setattr("duo_monitor.apple.time.monotonic", lambda: clock[0])
    c = AppleClient(["http://a:1", "http://b:1", "http://c:1"])
    a, b, cc = StatusExit(541), StatusExit(200), StatusExit(200)
    c._clients = {"http://a:1": a, "http://b:1": b, "http://c:1": cc}
    for _ in range(6):
        c._get("/x")
    assert a.n == 1 and b.n + cc.n == 6 and c.resting() == 1     # refused once, then left alone
    clock[0] += 301                                                 # first rest is 5 minutes
    a.status = 200
    for _ in range(3):
        c._get("/x")
    assert a.n == 2 and c.resting() == 0                            # back in rotation, and one success clears it


def test_rest_doubles_while_refused_and_all_resting_uses_the_one_back_soonest(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("duo_monitor.apple.time.monotonic", lambda: clock[0])
    c = AppleClient(["http://a:1", "http://b:1"])
    c._refused("http://a:1")
    clock[0] += 301
    c._refused("http://a:1")                                        # still refused on waking: 10 minutes this time
    assert c._rest["http://a:1"][1] == 600
    c._refused("http://b:1")                                        # b rests 5 min, until 601; a until 901
    assert c._pick() == "http://b:1"
    for _ in range(10):
        c._refused("http://a:1")
    assert c._rest["http://a:1"][1] == 900                          # capped at 15 min: back soon after a wave


def test_a_404_does_not_rest_the_exit():
    """Our own bad request is not the exit's fault."""
    c = AppleClient(["http://a:1"])
    c._clients = {"http://a:1": StatusExit(404)}
    with pytest.raises(httpx.HTTPStatusError):
        c._get("/x")
    assert c.resting() == 0


def test_plan_merges_watched_stores_apple_answers_together(monkeypatch):
    monkeypatch.setattr(poller, "nearby", {"R150": frozenset({"R150", "R040", "R080"})})
    targets = {"R150": {"MK1G4LL/A"}, "R040": {"MK1J4LL/A"}, "R300": {"MK1E4LL/A"}}
    assert sorted(poller.plan(targets)) == [("R150", ["MK1G4LL/A", "MK1J4LL/A"]), ("R300", ["MK1E4LL/A"])]
    many = {"R150": {f"A{i}" for i in range(6)}, "R040": {f"B{i}" for i in range(6)}}
    jobs = poller.plan(many)
    assert [st for st, _ in jobs] == ["R150", "R150"] and sorted(sum((p for _, p in jobs), [])) == sorted(
        many["R150"] | many["R040"])                                # 12 parts → two batches of ≤8, one anchor


class NearbyClient(FakeClient):
    """Answers like store=X&searchNearby=true: the asked store plus its neighbours."""
    def __init__(self, metro):
        super().__init__()
        self.metro = metro

    def availability(self, store, parts):
        self.calls.append((store, parts))
        return [Cell(st, "Store " + st, "City", p, "Phone " + p, self.stock.get((st, p), False), "q")
                for st in self.metro for p in parts]


def test_poll_learns_neighbours_then_one_request_covers_the_metro(conn, monkeypatch):
    monkeypatch.setattr(poller, "nearby", {})
    active_sub(conn, part="MK1G4LL/A", stores=("R150",))
    active_sub(conn, contact="+15015550124", part="MK1J4LL/A", stores=("R040",))
    client = NearbyClient(["R150", "R040", "R080"])
    poll_once(conn, client, FakeNotifier())
    assert len(client.calls) == 2                                   # first pass: each store teaches its neighbours
    client.calls.clear()
    client.stock[("R040", "MK1J4LL/A")] = True
    notifier = FakeNotifier()
    poll_once(conn, client, notifier)
    assert len(client.calls) == 1 and len(notifier.sent) == 1       # then one request, and the alert still goes out
    rows = {(r["store"], r["part"]) for r in conn.execute("SELECT store, part FROM availability")}
    assert rows == {("R150", "MK1G4LL/A"), ("R040", "MK1J4LL/A")}   # unwatched neighbour cells are not stored


class SlowClient(FakeClient):
    """Each request takes 50 ms; records how many were in flight at once. `fail_store` raises like a network error."""
    def __init__(self, fail_store=None):
        super().__init__()
        self.lock, self.inflight, self.peak, self.fail_store = threading.Lock(), 0, 0, fail_store

    def availability(self, store, parts):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(0.05)
            if store == self.fail_store:
                raise httpx.ConnectError("boom")
            return super().availability(store, parts)
        finally:
            with self.lock:
                self.inflight -= 1


def test_batches_are_fetched_concurrently_at_most_eight_at_a_time(conn):
    active_sub(conn, stores=tuple(f"R{n:03}" for n in range(20)))
    c = SlowClient()
    stats = poll_once(conn, c, FakeNotifier())
    assert stats["requests"] == 20 and 1 < c.peak <= poller.FETCH_WORKERS and "round_ms" in stats


def test_failed_fetch_only_skips_that_batch(conn):
    active_sub(conn, stores=("R300", "R030"))
    c, n = SlowClient(fail_store="R030"), FakeNotifier()
    c.stock[("R300", "MK1E4LL/A")] = c.stock[("R030", "MK1E4LL/A")] = True
    stats = poll_once(conn, c, n)
    assert (stats["errors"], stats["requests"], len(n.sent)) == (1, 1, 1)
    assert db.get_availability(conn, "R030", "MK1E4LL/A") is None


def test_alerts_go_out_after_the_pass_is_committed_and_hold_no_write_lock(tmp_path):
    path = str(tmp_path / "t.db")
    conn = db.connect(path)
    active_sub(conn, stores=("R300", "R030"))
    c = FakeClient()
    c.stock[("R300", "MK1E4LL/A")] = True
    seen = []

    class Peek(FakeNotifier):
        def alert(self, sub, text, subject, kind=None, sms=None):
            other = sqlite3.connect(path, timeout=0)        # a webhook writing while the alert is being sent
            seen.append(other.execute("SELECT COUNT(*) FROM availability").fetchone()[0])
            other.execute("INSERT INTO sms_queue (created_at, phone, text) VALUES ('x', 'y', 'z')")
            other.commit()                                  # raises "database is locked" if the poller held the lock
            other.close()
            return super().alert(sub, text, subject)

    poll_once(conn, c, Peek())
    assert seen == [2]                                      # both cells were written and committed before the message


def test_hooks_fire_as_before(conn, monkeypatch):
    events = []
    monkeypatch.setattr(hooks, "_handlers", {e: [lambda conn, _e=e, **kw: events.append(_e)]
                                             for e in ("cell.soldout", "alert.sent", "poll.pass")})
    active_sub(conn)
    c, n = FakeClient(), FakeNotifier()
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    c.stock[("R300", "MK1E4LL/A")] = False
    poll_once(conn, c, n)
    assert events == ["alert.sent", "poll.pass", "cell.soldout", "poll.pass"]


class _Stop(BaseException):
    pass


def test_run_forever_records_the_pass_and_sleeps_with_jitter(tmp_path, monkeypatch):
    from duo_monitor.config import Settings
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(poller, "last_pass_at", None)
    slept = []

    def sleep(sec):
        slept.append(sec)
        raise _Stop
    monkeypatch.setattr(poller.time, "sleep", sleep)
    monkeypatch.setattr(poller.random, "uniform", lambda a, b: b)
    with pytest.raises(_Stop):
        poller.run_forever(Settings())
    assert poller.last_pass_at is not None
    assert Settings().poll_seconds < slept[0] <= Settings().poll_seconds + poller.JITTER


# ---- honest delivery + retry cap ---------------------------------------------------------------------------------

class ScriptedNotifier(FakeNotifier):
    """Returns the scripted results in order, then keeps failing."""
    def __init__(self, results=()):
        super().__init__()
        self.results = list(results)

    def alert(self, sub, text, subject, kind=None, sms=None):
        super().alert(sub, text, subject)
        return self.results.pop(0) if self.results else (False, "failed: sms twilio 500")


def test_failing_channel_is_tried_at_most_three_times_per_restock(conn):
    active_sub(conn)
    c, n = FakeClient(), ScriptedNotifier()
    c.stock[("R300", "MK1E4LL/A")] = True
    for _ in range(5):
        poll_once(conn, c, n)
    assert len(n.sent) == db.MAX_ATTEMPTS == 3
    c.stock[("R300", "MK1E4LL/A")] = False
    poll_once(conn, c, n)
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    assert len(n.sent) == 4                                # a new restock is a new stretch: tried again


def test_retries_stop_after_a_delivered_attempt(conn):
    active_sub(conn)
    c, n = FakeClient(), ScriptedNotifier([(False, "failed: sms twilio 500"), (True, "sent: sms twilio 201")])
    c.stock[("R300", "MK1E4LL/A")] = True
    for _ in range(4):
        poll_once(conn, c, n)
    assert len(n.sent) == 2
    assert [r["ok"] for r in conn.execute("SELECT ok FROM alerts ORDER BY id")] == [0, 1]


SUB = {"id": "s1", "phone": "+15015550123", "email": "a@example.com", "lang": "en"}


@pytest.fixture
def bare_env(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_OUTBOX", "SMS_PULL_TOKEN",
              "RESEND_API_KEY", "EMAIL_FROM", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


def test_console_mode_is_ok_but_labeled_not_delivered(bare_env):
    from duo_monitor.config import Settings
    from duo_monitor.notify import Notifier
    assert Notifier(Settings()).alert(SUB, "hi", "subj") == (
        True, "console: sms (not delivered); console: email (not delivered)")


def test_provider_results_are_labeled_sent_or_failed(bare_env, monkeypatch):
    from duo_monitor import notify
    from duo_monitor.config import Settings
    s = Settings(twilio_sid="AC1", twilio_token="t", twilio_from="+18005550100", resend_key="k", email_from="a@getduonow.test")

    def post(url, **kw):
        if "twilio" in url:
            return httpx.Response(400)
        raise httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(notify.httpx, "post", post)
    assert notify.Notifier(s).alert(SUB, "hi", "subj") == (False, "failed: sms twilio 400; failed: email timed out")
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: httpx.Response(201 if "twilio" in url else 200))
    assert notify.Notifier(s).alert(SUB, "hi", "subj") == (True, "sent: sms twilio 201; sent: email resend 200")


def test_textbelt_backend_brands_and_routes_replies(bare_env, monkeypatch):
    from duo_monitor import notify
    from duo_monitor.config import Settings
    s = Settings(textbelt_key="k", base_url="https://getduonow.test")
    calls = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: (calls.append((url, kw["data"])),
                        httpx.Response(200, json={"success": True, "textId": "1", "quotaRemaining": 9}))[1])
    assert notify.Notifier(s).sms("+15015550123", "hi") == (True, "sent: sms textbelt 1")    # textId kept for status lookup
    url, data = calls[0]
    assert url == "https://textbelt.com/text" and data["phone"] == "5015550123" and data["sender"] == "GetDuoNow"
    assert data["replyWebhookUrl"] == "https://getduonow.test/textbelt/inbound"
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: httpx.Response(200, json={"success": False, "error": "Out of quota"}))
    assert notify.Notifier(s).sms("+15015550123", "hi") == (False, "failed: sms textbelt Out of quota")
    # Our own verified toll-free number wins once it exists.
    both = Settings(textbelt_key="k", twilio_sid="AC1", twilio_token="t", twilio_from="+18005550100")
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: httpx.Response(201 if "twilio" in url else 500))
    assert notify.Notifier(both).sms("+15015550123", "hi") == (True, "sent: sms twilio 201")


def test_textbelt_resends_without_links_until_the_key_is_whitelisted(bare_env, monkeypatch):
    """Before Textbelt verifies the key it rejects any link or email address; the alert must still go out."""
    from duo_monitor import notify
    from duo_monitor.config import Settings
    s = Settings(textbelt_key="SECRETKEY", base_url="https://getduonow.test")
    sent = []

    def post(url, **kw):
        sent.append(kw["data"]["message"])
        if "http" in kw["data"]["message"] or "@" in kw["data"]["message"]:
            return httpx.Response(200, json={"success": False, "error": "Sorry, ability to send URLs via text is limited "
                                             "to verified accounts. Please go to https://textbelt.com/whitelist?key=SECRETKEY"})
        return httpx.Response(200, json={"success": True, "textId": "2"})
    monkeypatch.setattr(notify.httpx, "post", post)
    ok, detail = notify.Notifier(s).sms("+15015550123", "GetDuoNow: in stock. Order: https://www.apple.com/shop/product/X Reply STOP to end")
    assert ok and detail.startswith("sent: sms textbelt") and "link removed" in detail
    assert sent[-1] == "GetDuoNow: in stock. Order: (see your email) Reply STOP to end"
    ok, detail = notify.Notifier(s).sms("+15015550123", "GetDuoNow：有货 马上下单 https://www.apple.com/x 回 STOP 退订")
    assert ok and sent[-1] == "GetDuoNow：有货 马上下单 （见邮件） 回 STOP 退订"
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: httpx.Response(200, json={
        "success": False, "error": "Out of quota, see https://textbelt.com/purchase?key=SECRETKEY"}))
    ok, detail = notify.Notifier(s).sms("+15015550123", "hi")
    assert not ok and "SECRETKEY" not in detail          # the error echoes the key: never store it in alerts/admin


def test_customer_replies_go_to_a_monitored_inbox(bare_env, monkeypatch):
    """The sending domain has no mailbox (no MX): refund / help replies must land somewhere a person reads."""
    from duo_monitor import notify
    from duo_monitor.config import Settings
    s = Settings(resend_key="k", email_from="a@getduonow.test", reply_to="help@getduonow.test")
    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: (sent.append(kw["json"]), httpx.Response(200))[1])
    assert notify.Notifier(s).email("x@example.com", "subj", "hi") == (True, "sent: email resend 200")
    assert sent[0]["reply_to"] == "help@getduonow.test"


def test_file_db_uses_wal_and_busy_timeout(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_a_crash_mid_pass_does_not_leave_the_write_lock_held(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    conn = db.connect(path)
    active_sub(conn, stores=("R300", "R030"))
    real, calls = db.upsert_availability, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*a, **kw)
    monkeypatch.setattr(db, "upsert_availability", flaky)
    with pytest.raises(sqlite3.OperationalError):
        poll_once(conn, FakeClient(), FakeNotifier())
    other = sqlite3.connect(path, timeout=0)
    other.execute("INSERT INTO sms_queue (created_at, phone, text) VALUES ('x', 'y', 'z')")
    other.commit()                                          # the webhook can still write: the pass was rolled back
    assert other.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def test_cells_are_stored_under_the_store_apple_reports(conn):
    """A response carries nearby stores' cells: each is recorded under its own store, never the requested one."""
    active_sub(conn)
    active_sub(conn, contact="+15015550124", stores=("R030",))

    class Nearby(FakeClient):
        def availability(self, store, parts):
            return [Cell("R300", "Riverside", "Springfield", p, "Phone", False, "Currently unavailable") for p in parts] + \
                   [Cell("R030", "Elsewhere", "Memphis", p, "Phone", True, "Available Today") for p in parts]
    poll_once(conn, Nearby(), FakeNotifier())
    assert db.get_availability(conn, "R300", "MK1E4LL/A")["available"] == 0
    assert db.get_availability(conn, "R030", "MK1E4LL/A")["available"] == 1


def test_restock_alerts_go_out_before_soldout_follow_ups_in_the_same_pass(conn, monkeypatch):
    events = []
    monkeypatch.setattr(hooks, "_handlers", {e: [lambda conn, _e=e, **kw: events.append(_e)]
                                             for e in ("cell.soldout", "alert.sent")})
    active_sub(conn, stores=("R300",))
    active_sub(conn, contact="+15015550124", stores=("R030",))
    c, n = FakeClient(), FakeNotifier()
    c.stock[("R300", "MK1E4LL/A")] = True
    poll_once(conn, c, n)
    events.clear()
    c.stock[("R300", "MK1E4LL/A")], c.stock[("R030", "MK1E4LL/A")] = False, True
    poll_once(conn, c, n)
    assert events == ["alert.sent", "cell.soldout"]       # the time-critical restock text is not queued behind follow-ups


def test_sms_alert_is_one_segment_in_english():
    from duo_monitor import messages
    t = messages.alert_sms("iPhone Duo 512GB Night Sky", "Riverside Mall", "MK1H4LL/A", "en")
    assert len(t) <= 160 and "STOP" in t and t.count("http") == 1
