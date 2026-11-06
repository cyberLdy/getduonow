"""Extend a watch by another pass without a login: signed link /extend/<sub_id>/<sig> (signing purpose "extend").

GET shows what is watched and until when; POST clones the watch into a new pending subscription (Stripe Checkout, or
activated at once when payments are off). When the clone activates it takes over: its expiry chains on from the old
pass (no lost days) and the old pass is expired (one active watch, no duplicate alerts).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, hooks, i18n, signing

router = APIRouter()
PURPOSE = "extend"


def setup(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS extensions (new_sub_id TEXT PRIMARY KEY, old_sub_id TEXT NOT NULL, "
                 "created_at TEXT NOT NULL)")
    conn.commit()


def _current(conn, sub_id: str, skip: str | None = None) -> str:
    """Follow activated extensions forward, so an extend link in an old message lands on the pass that carries the watch now."""
    while True:
        row = conn.execute("SELECT e.new_sub_id FROM extensions e JOIN subscriptions s ON s.id = e.new_sub_id "
                           "WHERE e.old_sub_id = ? AND e.new_sub_id IS NOT ? AND s.status != 'pending' "
                           "ORDER BY s.expires_at DESC LIMIT 1", (sub_id, skip)).fetchone()
        if not row:
            return sub_id
        sub_id = row["new_sub_id"]


def _verified_sub(sub_id: str, sig: str):
    from duo_monitor import web
    if not signing.verify(sub_id, PURPOSE, sig):
        raise HTTPException(404, "Not found")
    sub = db.get_subscription(web.conn, _current(web.conn, sub_id))
    if not sub:
        raise HTTPException(404, "Not found")
    return sub


@hooks.on("subscription.activated")
def take_over(conn, sub_id: str, **_) -> None:
    ext = conn.execute("SELECT old_sub_id FROM extensions WHERE new_sub_id=?", (sub_id,)).fetchone()
    new = db.get_subscription(conn, sub_id)
    if not ext or not new or not new["expires_at"]:
        return
    prev = db.get_subscription(conn, _current(conn, ext["old_sub_id"], skip=sub_id))
    if prev and prev["expires_at"]:
        # = max(old end, now) + watch_days, but added on top of what activate() set rather than overwriting it, so days
        # another activation handler already added (referral credit) survive whatever order the handlers run in
        left = max(datetime.fromisoformat(prev["expires_at"]) - db.now(), timedelta(0))
        conn.execute("UPDATE subscriptions SET expires_at=? WHERE id=?",
                     (db.iso(datetime.fromisoformat(new["expires_at"]) + left), sub_id))
    if prev:
        # the old pass was already alerted for a cell still in the same in-stock run: carry that over, so the new pass
        # (same person, same watches) is not alerted for it again on the next poll
        conn.execute("INSERT INTO alerts (sub_id, store, part, sent_at, ok, detail) "
                     "SELECT ?, a.store, a.part, MAX(a.sent_at), 1, ? FROM alerts a "
                     "JOIN availability v ON v.store = a.store AND v.part = a.part "
                     "JOIN watches w ON w.sub_id = ? AND w.store = a.store AND w.part = a.part "
                     "WHERE a.sub_id = ? AND a.ok = 1 AND v.available = 1 AND a.sent_at >= v.since "
                     "GROUP BY a.store, a.part", (sub_id, f"carried over from {prev['id']}", sub_id, prev["id"]))
    conn.commit()
    db.expire(conn, ext["old_sub_id"])
    if prev:
        db.expire(conn, prev["id"])


def _status(sub, lang: str) -> str:
    until = i18n.date(sub["expires_at"], lang)
    if sub["status"] == "active":
        return i18n.t("extend.statusActive", lang, date=until)
    if sub["status"] == "expired":
        if sub["expires_at"] and datetime.fromisoformat(sub["expires_at"]) > db.now():
            until = ""   # stopped early (unsubscribed / admin): its planned end date is not when it ended
        return i18n.t("extend.statusEnded", lang, date=until) if until else i18n.t("extend.statusEndedNoDate", lang)
    return i18n.t("extend.statusPending", lang)


PAGE = """<!doctype html>
<html {attrs}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><meta name="robots" content="noindex">
<style>
:root {{ --bg:#f5f5f2; --card:rgba(255,255,255,.78); --ink:#1d1d1f; --mute:#6e6e73; --faint:#a1a1a6; --line:#e3e2dd;
  --ok:#2f7a55; --ok-bg:rgba(47,122,85,.10); --sel:#1d1d1f; --sel-ink:#fff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b0f17; --card:rgba(255,255,255,.06); --ink:#f4f6fa; --mute:#a3a8b3;
  --faint:#6b7180; --line:rgba(255,255,255,.12); --ok:#9fdcb9; --ok-bg:rgba(159,220,185,.12); --sel:#f4f6fa; --sel-ink:#0b0f17; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text",
  "Segoe UI", "PingFang SC", sans-serif; -webkit-font-smoothing:antialiased; }}
main {{ max-width:620px; margin:0 auto; padding:20px 16px 72px; }}
.top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:28px; }}
.brand {{ font-weight:700; letter-spacing:-0.02em; font-size:19px; color:inherit; text-decoration:none; }}
.langsel {{ font:inherit; font-size:14px; color:inherit; background:transparent; border:1px solid var(--line); border-radius:999px; padding:6px 10px; max-width:150px; }}
h1 {{ font-size:34px; line-height:1.08; letter-spacing:-0.03em; margin:0 0 10px; }}
.sub {{ color:var(--mute); margin:0 0 22px; font-size:17px; }}
.card {{ background:var(--card); backdrop-filter:blur(20px); border:1px solid var(--line); border-radius:22px; padding:18px; margin-bottom:12px; }}
.status {{ display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:600; color:var(--ok); background:var(--ok-bg);
  padding:4px 10px; border-radius:999px; margin-bottom:12px; }}
.status.off {{ color:var(--mute); background:transparent; border:1px solid var(--line); }}
.store {{ padding:10px 0; border-top:1px solid var(--line); }} .store:first-of-type {{ border-top:0; }}
.store b {{ display:block; }} .store span {{ color:var(--mute); font-size:15px; }}
.btn {{ width:100%; border:0; border-radius:14px; padding:15px; font:inherit; font-size:17px; font-weight:600;
  background:var(--sel); color:var(--sel-ink); cursor:pointer; }}
.more {{ display:block; text-align:center; margin-top:14px; color:var(--mute); text-underline-offset:3px; }}
.fine {{ color:var(--faint); font-size:12.5px; margin-top:22px; line-height:1.55; }}
</style></head><body><main>
<div class="top"><a class="brand" href="/">GetDuoNow</a>
{switcher}</div>
<h1>{h1}</h1><p class="sub">{lead}</p>
<div class="card"><div class="status{status_cls}">{status}</div>{stores}</div>
<form method="post"><button class="btn" type="submit">{button}</button></form>
<a class="more" href="/">{more}</a>
<p class="fine">{fine}</p>
</main></body></html>"""


@router.get("/extend/{sub_id}/{sig}", response_class=HTMLResponse)
def extend_page(sub_id: str, sig: str, request: Request) -> str:
    from duo_monitor import web
    sub = _verified_sub(sub_id, sig)
    lang, days, price = i18n.pick(request, sub["lang"]), web.s.watch_days, f"${web.s.price_cents / 100:.2f}"
    by_store: dict[str, list[str]] = {}
    for w in db.get_watches(web.conn, sub["id"]):
        by_store.setdefault(w["store_name"], []).append(w["product"])
    stores = "".join(f'<div class="store"><b>Apple {escape(name)}</b><span>{escape(", ".join(products))}</span></div>'
                     for name, products in by_store.items())
    button = i18n.t("extend.buttonPay", lang, days=days, price=price) if web.s.payments_on else i18n.t("extend.button", lang)
    return PAGE.format(
        attrs=i18n.html_attrs(lang), switcher=i18n.switcher(lang, request.url.path),
        title=escape(i18n.t("extend.title", lang)), h1=escape(i18n.t("extend.h1", lang)),
        lead=escape(i18n.t("extend.lead", lang, days=days)),
        status_cls="" if sub["status"] == "active" else " off", status=escape(_status(sub, lang)), stores=stores,
        button=escape(button), more=escape(i18n.t("extend.more", lang)), fine=escape(i18n.t("common.notAffiliated", lang)))


@router.post("/extend/{sub_id}/{sig}")
def extend(sub_id: str, sig: str) -> RedirectResponse:
    from duo_monitor import web
    old = _verified_sub(sub_id, sig)
    watches = [(w["store"], w["store_name"], w["part"], w["product"]) for w in db.get_watches(web.conn, old["id"])]
    new_id = db.create_subscription(web.conn, email=old["email"], phone=old["phone"], carrier=old["carrier"],
                                    watches=watches, lang=old["lang"])
    web.conn.execute("INSERT INTO extensions (new_sub_id, old_sub_id, created_at) VALUES (?, ?, ?)",
                     (new_id, old["id"], db.iso(db.now())))
    # same number, same models and stores: a YES given for the watch being extended still holds
    web.conn.execute("UPDATE subscriptions SET sms_ok_at=? WHERE id=?", (old["sms_ok_at"], new_id))
    web.conn.commit()
    if web.s.payments_on:
        url = web.create_checkout(new_id, old["email"], watches, title=f"GetDuoNow +{web.s.watch_days} days",
                                  lang=old["lang"])
        return RedirectResponse(url, status_code=303)
    db.activate(web.conn, new_id, web.s.watch_days)
    hooks.emit("subscription.activated", web.conn, sub_id=new_id, via="free")   # take_over() chains the expiry
    web._welcome(new_id)
    return RedirectResponse(f"/done?sub={new_id}&sig={signing.sign(new_id, 'watch')}", status_code=303)
