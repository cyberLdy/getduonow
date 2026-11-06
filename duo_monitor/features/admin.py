"""Owner dashboard + event log.

Every hook event (subscription.created / .activated, alert.sent, subscriptions.expired, cell.soldout) lands in
`events`. /admin shows overview numbers, all subscriptions and the latest events; /admin/user?email= is one
customer's history; /admin/export.csv dumps subscriptions with counts.
Auth: open /admin?token=<ADMIN_TOKEN> once (sets an httponly cookie), or send X-Admin-Token. No ADMIN_TOKEN → 404.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import timedelta
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from duo_monitor import db, hooks

router = APIRouter()
COOKIE = "gdn_admin"
LOGGED = ("subscription.created", "subscription.activated", "alert.sent", "subscriptions.expired", "cell.soldout",
          "welcome.sent", "sms.inbound")
NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


def setup(conn) -> None:
    conn.executescript("""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    sub_id TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS events_sub ON events (sub_id);
""")


def record(conn, event: str, sub_id: str | None = None, **data) -> None:
    conn.execute("INSERT INTO events (at, event, sub_id, data) VALUES (?, ?, ?, ?)",
                 (db.iso(db.now()), event, sub_id, json.dumps(data, default=str, ensure_ascii=False)))
    conn.commit()


def _logger(event: str):
    def handler(conn, sub_id: str | None = None, **data) -> None:
        record(conn, event, sub_id, **data)
    handler.__name__ = "log_" + event.replace(".", "_")
    return handler


for _event in LOGGED:
    hooks.on(_event)(_logger(_event))


# ---------- auth ----------

def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _web():
    from duo_monitor import web
    if not web.s.admin_token:
        raise HTTPException(404)
    return web


def _authed(request: Request):
    web = _web()
    tok = web.s.admin_token
    if _same(request.headers.get("x-admin-token", ""), tok) or _same(request.cookies.get(COOKIE, ""), tok):
        return web
    raise HTTPException(401, "admin only")


# ---------- queries ----------

def stats(conn, price_cents: int) -> dict:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]   # noqa: E731
    by_status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM subscriptions GROUP BY status")}
    # paid = activated through a Stripe checkout (activate() sets expires_at; comps/free never get a session)
    paid = one("SELECT COUNT(*) FROM subscriptions WHERE checkout_session IS NOT NULL AND expires_at IS NOT NULL")
    now = db.now()
    d1, d7 = db.iso(now - timedelta(days=1)), db.iso(now - timedelta(days=7))
    return {"active": by_status.get("active", 0), "pending": by_status.get("pending", 0),
            "expired": by_status.get("expired", 0), "paid": paid, "revenue_cents": paid * price_cents,
            "sent_24h": one("SELECT COUNT(*) FROM alerts WHERE ok=1 AND sent_at >= ?", d1),
            "sent_7d": one("SELECT COUNT(*) FROM alerts WHERE ok=1 AND sent_at >= ?", d7),
            "failed_7d": one("SELECT COUNT(*) FROM alerts WHERE ok=0 AND sent_at >= ?", d7),
            "customers": one("SELECT COUNT(DISTINCT lower(email)) FROM subscriptions WHERE COALESCE(email, '') != ''")}


def subscriptions(conn, email: str | None = None, limit: int = 100000) -> list[dict]:
    """Subscriptions newest first, each with #watches, store names and alert counts."""
    where, args = ("WHERE lower(s.email) = ?", (email.lower(),)) if email else ("", ())
    rows = conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM watches w WHERE w.sub_id = s.id) AS n_watches, "
        "(SELECT COUNT(*) FROM alerts a WHERE a.sub_id = s.id AND a.ok = 1) AS n_alerts, "
        "(SELECT COUNT(*) FROM alerts a WHERE a.sub_id = s.id AND a.ok = 0) AS n_failed "
        f"FROM subscriptions s {where} ORDER BY s.created_at DESC LIMIT ?", (*args, limit)).fetchall()
    stores: dict[str, list[str]] = {}
    for r in conn.execute("SELECT DISTINCT sub_id, store_name FROM watches ORDER BY store_name"):
        stores.setdefault(r["sub_id"], []).append(r["store_name"])
    return [{**dict(r), "stores": stores.get(r["id"], [])} for r in rows]


# ---------- page chrome ----------

T = {
    "en": {"title": "Owner dashboard", "active": "Active", "pending": "Pending", "expired": "Expired",
           "paid": "Paid", "revenue": "Revenue", "sent_24h": "Alerts 24h", "sent_7d": "Alerts 7d",
           "failed_7d": "Failed 7d", "customers": "Customers", "subs": "Subscriptions", "events": "Latest events",
           "created": "Created", "status": "Status", "email": "Email", "phone": "Phone", "carrier": "Carrier",
           "lang": "Lang", "watches": "Watches", "stores": "Stores", "expires": "Expires", "alerts": "Alerts",
           "time": "Time", "event": "Event", "details": "Details", "store": "Store", "product": "Model",
           "result": "Result", "timeline": "Timeline", "customer": "Customer", "export": "Export CSV",
           "back": "← Overview", "none": "Nothing yet", "utc": "Times in UTC", "ok": "sent", "fail": "failed"},
    "zh": {"title": "后台总览", "active": "生效中", "pending": "待付款", "expired": "已过期",
           "paid": "付费开通", "revenue": "收入", "sent_24h": "24 小时通知", "sent_7d": "7 天通知",
           "failed_7d": "7 天失败", "customers": "客户数", "subs": "订阅", "events": "最新事件",
           "created": "创建", "status": "状态", "email": "邮箱", "phone": "手机", "carrier": "运营商",
           "lang": "语言", "watches": "监控", "stores": "门店", "expires": "到期", "alerts": "通知",
           "time": "时间", "event": "事件", "details": "详情", "store": "门店", "product": "型号",
           "result": "结果", "timeline": "事件时间线", "customer": "客户", "export": "导出 CSV",
           "back": "← 总览", "none": "暂无", "utc": "时间为 UTC", "ok": "已发", "fail": "失败"},
}

CSS = """
:root{--bg:#f5f5f2;--card:rgba(255,255,255,.78);--ink:#1d1d1f;--mute:#6e6e73;--faint:#a1a1a6;--line:#e3e2dd;
--ok:#2f7a55;--ok-bg:rgba(47,122,85,.10);--err:#b3261e;--err-bg:rgba(179,38,30,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f17;--card:rgba(255,255,255,.06);--ink:#f4f6fa;--mute:#a3a8b3;
--faint:#6b7180;--line:rgba(255,255,255,.12);--ok:#9fdcb9;--ok-bg:rgba(159,220,185,.12);--err:#ff8a80;--err-bg:rgba(255,138,128,.10)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","PingFang SC",sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1100px;margin:0 auto;padding:20px 16px 64px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:22px;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:-.02em;font-size:19px}.brand small{color:var(--mute);font-weight:500;margin-left:8px}
.nav{display:flex;gap:14px;font-size:14px}.nav a{color:var(--mute)}
h1{font-size:30px;letter-spacing:-.03em;margin:0 0 14px;overflow-wrap:anywhere}h2{font-size:13px;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.08em;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card);backdrop-filter:blur(20px);border:1px solid var(--line);border-radius:22px;padding:16px;margin-bottom:12px}
.stat .n{font-size:28px;font-weight:700;letter-spacing:-.03em;overflow-wrap:anywhere}.stat .l{color:var(--mute);font-size:13px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{text-align:left;color:var(--mute);font-weight:600;font-size:12px;padding:6px 10px 8px 0;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px 8px 0;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}.nw{white-space:nowrap}.mute{color:var(--mute)}
a{color:inherit;text-underline-offset:3px}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.pill.active,.pill.ok{color:var(--ok);background:var(--ok-bg);border-color:transparent}
.pill.fail{color:var(--err);background:var(--err-bg);border-color:transparent}
.empty{color:var(--faint)}code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mute);overflow-wrap:break-word}
"""


def _lang(request: Request) -> str:
    q = request.query_params.get("lang", "")
    if q in T:
        return q
    return "zh" if "zh" in request.headers.get("accept-language", "").lower() else "en"


def _t(ts: str | None) -> str:
    return escape(ts[:16].replace("T", " ")) if ts else "—"


def _pill(text: str, cls: str) -> str:
    return f'<span class="pill {escape(cls)}">{escape(text)}</span>'


def _user_link(email: str | None) -> str:
    return f'<a href="/admin/user?email={quote(email)}">{escape(email)}</a>' if email else "—"


def _table(head: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _page(title: str, lang: str, body: str, back: bool = False, keep: str = "") -> HTMLResponse:
    L = T[lang]
    other = "en" if lang == "zh" else "zh"
    nav = (f'<a href="/admin?lang={lang}">{L["back"]}</a>' if back else
           f'<a href="/admin/export.csv">{L["export"]}</a>') + f'<a href="?{keep}lang={other}">{"EN" if other == "en" else "中文"}</a>'
    html = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{escape(title)} · GetDuoNow</title><style>{CSS}</style></head><body><main>'
            f'<div class="top"><div class="brand">GetDuoNow<small>admin</small></div><div class="nav">{nav}</div></div>'
            f'{body}<p class="mute" style="font-size:12px">{L["utc"]}</p></main></body></html>')
    return HTMLResponse(html, headers=NO_STORE)


def _event_rows(rows) -> list[list[str]]:
    out = []
    for e in rows:
        data = json.loads(e["data"] or "{}")
        detail = " · ".join(f"{k}={v}" for k, v in data.items())
        who = _user_link(e["email"]) if "email" in e.keys() and e["email"] else (f"<code>{escape(e['sub_id'][:8])}</code>" if e["sub_id"] else "")
        out.append([f'<span class="nw">{_t(e["at"])}</span>', escape(e["event"]), who, f"<code>{escape(detail)}</code>"])
    return out


# ---------- routes ----------

@router.get("/admin", response_class=HTMLResponse)
def overview(request: Request, token: str = ""):
    if token:   # one-time login link: swap the token for a cookie, then drop it from the URL
        web = _web()
        if not _same(token, web.s.admin_token):
            raise HTTPException(401, "admin only")
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=30 * 86400, path="/admin", httponly=True, secure=True, samesite="lax")
        return resp
    web = _authed(request)
    lang = _lang(request)
    L = T[lang]
    st = stats(web.conn, web.s.price_cents)
    cards = [("active", st["active"]), ("pending", st["pending"]), ("expired", st["expired"]), ("paid", st["paid"]),
             ("revenue", f"${st['revenue_cents'] / 100:,.2f}"), ("sent_24h", st["sent_24h"]), ("sent_7d", st["sent_7d"]),
             ("failed_7d", st["failed_7d"]), ("customers", st["customers"])]
    grid = "".join(f'<div class="card stat"><div class="n">{escape(str(v))}</div><div class="l">{L[k]}</div></div>' for k, v in cards)
    subs = [[f'<span class="nw">{_t(x["created_at"])}</span>', _pill(L.get(x["status"], x["status"]), x["status"]),
             _user_link(x["email"]), escape(("…" + x["phone"][-4:]) if x["phone"] else "—"), escape(x["carrier"]),
             escape(x["lang"]), str(x["n_watches"]), escape(" · ".join(x["stores"])),
             f'<span class="nw">{_t(x["expires_at"])}</span>',
             str(x["n_alerts"]) + (f' <span class="pill fail">{x["n_failed"]}</span>' if x["n_failed"] else "")]
            for x in subscriptions(web.conn, limit=500)]
    events = web.conn.execute("SELECT e.*, s.email FROM events e LEFT JOIN subscriptions s ON s.id = e.sub_id "
                              "ORDER BY e.id DESC LIMIT 50").fetchall()
    body = (f'<h1>{L["title"]}</h1><div class="grid">{grid}</div>'
            f'<div class="card"><h2>{L["subs"]}</h2>' + _table(
                [L[k] for k in ("created", "status", "email", "phone", "carrier", "lang", "watches", "stores", "expires", "alerts")],
                subs, L["none"]) + '</div>'
            f'<div class="card"><h2>{L["events"]}</h2>' + _table(
                [L["time"], L["event"], L["customer"], L["details"]], _event_rows(events), L["none"]) + '</div>')
    return _page(L["title"], lang, body)


@router.get("/admin/user", response_class=HTMLResponse)
def user_page(request: Request, email: str = ""):
    web = _authed(request)
    lang = _lang(request)
    L = T[lang]
    email = email.strip().lower()
    subs = subscriptions(web.conn, email=email) if email else []
    ids = "SELECT id FROM subscriptions WHERE lower(email) = ?"
    watches = web.conn.execute(f"SELECT * FROM watches WHERE sub_id IN ({ids}) ORDER BY store_name, product", (email,)).fetchall()
    alerts = web.conn.execute(
        "SELECT a.*, w.store_name, w.product FROM alerts a LEFT JOIN watches w "
        f"ON w.sub_id = a.sub_id AND w.store = a.store AND w.part = a.part WHERE a.sub_id IN ({ids}) "
        "ORDER BY a.sent_at DESC LIMIT 300", (email,)).fetchall()
    events = web.conn.execute(f"SELECT * FROM events WHERE sub_id IN ({ids}) ORDER BY id DESC LIMIT 300", (email,)).fetchall()
    sub_rows = [[f"<code>{escape(x['id'][:8])}</code>", f'<span class="nw">{_t(x["created_at"])}</span>',
                 _pill(L.get(x["status"], x["status"]), x["status"]), escape(x["phone"] or "—"), escape(x["carrier"]),
                 escape(x["lang"]), f'<span class="nw">{_t(x["expires_at"])}</span>', str(x["n_alerts"])] for x in subs]
    watch_rows = [[f"<code>{escape(w['sub_id'][:8])}</code>", escape(w["store_name"]) + f' <span class="mute">{escape(w["store"])}</span>',
                   escape(w["product"])] for w in watches]
    alert_rows = [[f'<span class="nw">{_t(a["sent_at"])}</span>', escape(a["store_name"] or a["store"]), escape(a["product"] or a["part"]),
                   _pill(L["ok"], "ok") if a["ok"] else _pill(L["fail"], "fail"), f"<code>{escape(a['detail'] or '')}</code>"]
                  for a in alerts]
    body = (f'<h1>{escape(email) or L["customer"]}</h1>'
            f'<div class="card"><h2>{L["subs"]}</h2>' + _table(
                ["ID", L["created"], L["status"], L["phone"], L["carrier"], L["lang"], L["expires"], L["alerts"]], sub_rows, L["none"]) +
            f'</div><div class="card"><h2>{L["watches"]}</h2>' + _table(["ID", L["store"], L["product"]], watch_rows, L["none"]) +
            f'</div><div class="card"><h2>{L["alerts"]}</h2>' + _table(
                [L["time"], L["store"], L["product"], L["result"], L["details"]], alert_rows, L["none"]) +
            f'</div><div class="card"><h2>{L["timeline"]}</h2>' + _table(
                [L["time"], L["event"], "ID", L["details"]], _event_rows(events), L["none"]) + '</div>')
    return _page(email or L["customer"], lang, body, back=True, keep=f"email={quote(email)}&amp;")


def _cell(v) -> str:
    """Spreadsheet-safe cell: user-typed text starting with = + - @ must not run as a formula."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@router.get("/admin/export.csv")
def export_csv(request: Request) -> Response:
    web = _authed(request)
    cols = ["id", "created_at", "status", "email", "phone", "carrier", "lang", "expires_at", "checkout_session",
            "n_watches", "stores", "n_alerts", "n_failed"]
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(cols)
    for x in subscriptions(web.conn):
        out.writerow([_cell(" | ".join(x["stores"]) if c == "stores" else x[c]) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={**NO_STORE, "Content-Disposition": 'attachment; filename="getduonow-subscriptions.csv"'})
