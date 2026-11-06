"""Live log for one watch (signup step ⑤): what we check, each store × model's current state, and what happened.

GET /api/watch/{sub_id}/{sig} with sig = signing.sign(sub_id, "watch") (the /done link carries it). No personal data.
Restocks are recorded when a cell sells out (cell.soldout carries since + minutes), so the log can say
"was in stock for 28 min" for every restock at a watched store, alerted or not.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from duo_monitor import db, hooks, i18n, signing

router = APIRouter()
MAX_EVENTS = 50


def setup(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS restocks (store TEXT NOT NULL, part TEXT NOT NULL, store_name TEXT, "
                 "started_at TEXT NOT NULL, minutes INTEGER NOT NULL, PRIMARY KEY (store, part, started_at))")
    conn.commit()


@hooks.on("cell.soldout")
def _record_restock(conn, store: str, part: str, store_name: str = "", since: str = "", minutes: int = 0, **_) -> None:
    conn.execute("INSERT OR IGNORE INTO restocks (store, part, store_name, started_at, minutes) VALUES (?, ?, ?, ?, ?)",
                 (store, part, store_name, since, minutes))
    conn.commit()


def log_url(sub_id: str) -> str:
    from duo_monitor import web
    return f"{web.s.base_url.rstrip('/')}/done?sub={sub_id}&sig={signing.sign(sub_id, 'watch')}"


@hooks.footer
def _log_footer(sub, kind: str, lang: str) -> str:
    """Welcome email: where to check the watch any time (step ⑤)."""
    return i18n.t("watchlog.footer", lang, url=log_url(sub["id"])) if kind == "welcome" else ""


@router.get("/api/watch/{sub_id}/{sig}")
def watch_log(sub_id: str, sig: str) -> dict:
    from duo_monitor import poller, web
    sub = db.get_subscription(web.conn, sub_id) if signing.verify(sub_id, "watch", sig) else None
    if not sub:
        raise HTTPException(404, "Not found")
    if sub["status"] == "pending":   # just back from Stripe: confirm the payment ourselves instead of waiting on the webhook
        web.confirm_payment(sub_id)
        sub = db.get_subscription(web.conn, sub_id)
    watches = db.get_watches(web.conn, sub_id)
    product = {(w["store"], w["part"]): w["product"] for w in watches}
    store_name = {w["store"]: w["store_name"] for w in watches}
    cells = []
    for w in watches:
        a = db.get_availability(web.conn, w["store"], w["part"])
        cells.append({"store_name": w["store_name"], "product": w["product"],
                      "available": bool(a and a["available"]), "since": a["since"] if a else None})
    events = [{"at": sub["created_at"], "kind": "start"}]
    for r in web.conn.execute("SELECT store, part, sent_at, ok FROM alerts WHERE sub_id=? ORDER BY sent_at", (sub_id,)):
        events.append({"at": r["sent_at"], "kind": "alert" if r["ok"] else "alert_fail",
                       "store_name": store_name.get(r["store"], r["store"]), "product": product.get((r["store"], r["part"]))})
    for r in web.conn.execute("SELECT * FROM restocks WHERE started_at >= ? ORDER BY started_at", (sub["created_at"],)):
        if (r["store"], r["part"]) in product:
            events.append({"at": r["started_at"], "kind": "restock", "store_name": store_name[r["store"]],
                           "product": product[(r["store"], r["part"])], "minutes": r["minutes"]})
    events.sort(key=lambda e: e["at"], reverse=True)
    last = poller.last_pass_at
    sms = None if not sub["phone"] else {"state": "on" if sub["sms_ok_at"] else "waiting", "last4": sub["phone"][-4:]}
    return {"status": sub["status"], "expires_at": sub["expires_at"], "paid": sub["status"] != "pending" and bool(sub["checkout_session"]),
            "sms": sms, "checking": bool(sub["email"] or sub["sms_ok_at"]),   # text-only: checks start at YES
            "last_check_s": round(time.time() - last) if last else None, "cells": cells, "events": events[:MAX_EVENTS]}


def _masked(name: str) -> str:
    """Public social proof never names the store (a named store draws resellers to it): same length, no letters."""
    return "Apple " + "•" * max(6, min(14, len(name or "")))


@router.get("/api/restocks/recent")
def recent_restocks(limit: int = 20) -> dict:
    """Real restocks we caught (public social proof): store, model part, when, how long it lasted. No customer data."""
    from duo_monitor import web
    rows = web.conn.execute("SELECT store_name, part, started_at, minutes FROM restocks ORDER BY started_at DESC LIMIT ?",
                            (max(1, min(limit, 50)),)).fetchall()
    agg = web.conn.execute("SELECT COUNT(*) AS n, MIN(started_at) AS since FROM restocks").fetchone()
    # "In stock right now" only for cells the poller refreshes every pass (watched by an active watch); a cell nobody
    # watches keeps its last state forever and would claim stock that is long gone.
    watched = db.watch_targets(web.conn)
    live = [r for r in web.conn.execute("SELECT store, store_name, part, since AS started_at FROM availability "
                                        "WHERE available=1 ORDER BY since DESC").fetchall()
            if r["part"] in watched.get(r["store"], ())][:5]
    live = [{k: r[k] for k in ("store_name", "part", "started_at")} for r in live]
    from duo_monitor import poller
    last = poller.last_pass_at
    items = [{**dict(r), "store_name": _masked(r["store_name"])} for r in rows]
    live = [{**r, "store_name": _masked(r["store_name"])} for r in live]
    return {"count": agg["n"], "since": agg["since"], "items": items, "live": live,
            "last_check_s": round(time.time() - last) if last else None}


@router.post("/admin/restocks/import")
async def import_restocks(request: Request) -> dict:
    """Admin: add restocks caught before this table existed (e.g. the pre-launch monitor's log). Idempotent."""
    from duo_monitor import web
    if not web._is_admin(request):
        raise HTTPException(401, "admin only")
    n = 0
    for r in await request.json():
        cur = web.conn.execute("INSERT OR IGNORE INTO restocks (store, part, store_name, started_at, minutes) VALUES (?, ?, ?, ?, ?)",
                               (r["store"], r["part"], r["store_name"], r["started_at"], int(r["minutes"])))
        n += cur.rowcount
    web.conn.commit()
    return {"imported": n}
