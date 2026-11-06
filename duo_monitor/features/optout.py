"""Opt-out: signed unsubscribe links (page + RFC 8058 one-click), List-Unsubscribe email headers, an unsubscribe line
in every alert/welcome, and inbound SMS commands (STOP / HELP / START) from the Mac relay or a Twilio webhook.

    POST /api/sms-inbound   X-Token: SMS_PULL_TOKEN, JSON {phone, text}   — scripts/sms_relay.py forwards replies here
    POST /twilio/inbound    Twilio messaging webhook (X-Twilio-Signature checked with TWILIO_AUTH_TOKEN)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from string import Template
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .. import db, hooks, i18n, signing

router = APIRouter()
PAGE = Template((Path(__file__).parent.parent / "templates" / "unsub.html").read_text())

STOP = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "退订", "取消"}
HELP = {"help", "帮助"}
START = {"start", "y", "yes", "unstop"}
# YES to the confirmation text, in any of our languages → the language it is written in (None = any language).
# A reply in another language than the watch's switches that watch's texts to it.
YES = {"yes": None, "y": None, "ok": None, "okay": None, "yep": None, "yeah": None, "sure": None, "confirm": None,
       "👍": None, "si": "es", "sí": "es", "claro": "es", "是": "zh", "是的": "zh", "好": "zh", "好的": "zh", "确认": "zh",
       "確認": "zh-Hant", "同意": "zh", "可以": "zh", "嗯": "zh", "có": "vi", "co": "vi", "vâng": "vi", "đồng ý": "vi",
       "dong y": "vi", "ừ": "vi", "네": "ko", "예": "ko", "응": "ko", "확인": "ko", "좋아요": "ko", "はい": "ja", "うん": "ja",
       "了解": "ja", "承認": "ja", "نعم": "ar", "أجل": "ar", "موافق": "ar", "بله": "fa", "آره": "fa", "باشه": "fa",
       "موافقم": "fa"}
def setup(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS sms_consent (phone TEXT PRIMARY KEY, status TEXT NOT NULL, at TEXT NOT NULL)")
    conn.commit()


def opt_out_phone(conn, phone: str) -> int:
    """Expire every pending/active subscription on this phone; → how many."""
    cur = conn.execute("UPDATE subscriptions SET status='expired' WHERE phone=? AND status IN ('pending', 'active')", (phone,))
    conn.commit()
    return cur.rowcount


def opt_out_sub(conn, sub_id: str) -> bool:
    return db.expire(conn, sub_id)


def _unsub_link(sub) -> str:
    from duo_monitor import web
    return signing.link(web.s.base_url, "unsub", sub["id"])


@hooks.email_header
def _list_unsubscribe(sub) -> dict:
    return {"List-Unsubscribe": f"<{_unsub_link(sub)}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


@hooks.footer
def _unsub_footer(sub, kind: str, lang: str) -> str:
    if kind not in ("alert", "welcome"):
        return ""
    return i18n.t("optout.footer", lang, url=_unsub_link(sub))


# ---- unsubscribe page -------------------------------------------------------------------------------------------

def _page(request: Request, sub, done: bool) -> HTMLResponse:
    lang = i18n.pick(request, sub["lang"])
    tt = lambda k: i18n.t(f"optout.{k}", lang)  # noqa: E731
    form = "" if done else f'<form method="post"><button class="btn" type="submit">{tt("btn")}</button></form>'
    again = f'<a class="again" href="/?lang={lang}">{tt("again")}</a>' if done else ""
    return HTMLResponse(PAGE.substitute(
        attrs=i18n.html_attrs(lang), title=tt("doneT") if done else tt("askT"), body=tt("doneB") if done else tt("askB"),
        form=form, again=again, switcher=i18n.switcher(lang, request.url.path), fine=i18n.t("common.notAffiliated", lang)))


def _checked_sub(sub_id: str, sig: str):
    sub = db.get_subscription(_conn(), sub_id) if signing.verify(sub_id, "unsub", sig) else None
    if sub is None:
        raise HTTPException(404, "Not found")
    return sub


def _conn():
    from duo_monitor import web
    return web.conn


@router.get("/unsub/{sub_id}/{sig}", response_class=HTMLResponse)
def unsub_page(sub_id: str, sig: str, request: Request) -> HTMLResponse:
    sub = _checked_sub(sub_id, sig)
    return _page(request, sub, done=sub["status"] == "expired")


@router.post("/unsub/{sub_id}/{sig}", response_class=HTMLResponse)
def unsub_confirm(sub_id: str, sig: str, request: Request) -> HTMLResponse:
    """The page's button and RFC 8058 one-click (body List-Unsubscribe=One-Click) both land here: expire, 200."""
    sub = _checked_sub(sub_id, sig)
    opt_out_sub(_conn(), sub_id)
    return _page(request, sub, done=True)


# ---- inbound SMS ------------------------------------------------------------------------------------------------

def _sub_lang(conn, phone: str) -> str:
    row = conn.execute("SELECT lang FROM subscriptions WHERE phone=? ORDER BY created_at DESC LIMIT 1", (phone,)).fetchone()
    return row["lang"] if row else "en"


def _set_consent(conn, phone: str, status: str) -> None:
    conn.execute("INSERT INTO sms_consent (phone, status, at) VALUES (?, ?, ?) "
                 "ON CONFLICT(phone) DO UPDATE SET status=excluded.status, at=excluded.at", (phone, status, db.iso(db.now())))
    conn.commit()


def _confirm(conn, phone: str, said: str | None) -> str | None:
    """YES to the confirmation text → the reply listing what now texts, or None when nothing was waiting."""
    from duo_monitor import messages
    ids = db.confirm_sms(conn, phone)
    if not ids:
        return None
    _set_consent(conn, phone, "on")
    lang = _sub_lang(conn, phone)
    if said and said.split("-")[0] != lang.split("-")[0]:   # answered in another language: speak that one from now on
        conn.executemany("UPDATE subscriptions SET lang=? WHERE id=?", [(said, i) for i in ids])
        conn.commit()
        lang = said
    watches = [w for i in ids for w in db.get_watches(conn, i)]
    return messages.confirmed_sms(watches, lang)


def handle_command(conn, phone: str, text: str) -> tuple[str, str]:
    """→ (action, reply; "" = no reply). Commands are whole messages, case-insensitive, trimmed."""
    from duo_monitor import web
    cmd = text.strip().strip(".!?。！？ ").strip().lower()
    if cmd in YES:
        reply = _confirm(conn, phone, YES[cmd])
        if reply:
            return "confirm", reply
        cmd = "start"                 # nothing waiting: 是 / はい / 네 … is consent, the same as YES
    lang = _sub_lang(conn, phone)
    if not cmd.isascii() and not lang.startswith("zh"):     # 退订 / 帮助 from a non-Chinese watch: answer in Chinese
        lang = "zh"
    if cmd in STOP:
        opt_out_phone(conn, phone)
        _set_consent(conn, phone, "stopped")
        action = "stop"
    elif cmd in HELP:
        action = "help"
    elif cmd in START:
        _set_consent(conn, phone, "on")     # consent only: expired watches stay expired (they were paid passes)
        action = "start"
    else:
        return "ignored", ""
    return action, i18n.t(f"optout.reply{action.title()}", lang, contact=web.s.reply_to or web.s.base_url)


def _phone(raw: str) -> str | None:
    from duo_monitor import web
    try:
        return web.normalize_phone(raw)
    except HTTPException:
        return None


class InboundIn(BaseModel):
    phone: str
    text: str


@router.post("/api/sms-inbound")
def sms_inbound(body: InboundIn, request: Request) -> dict:
    """Replies to our texts, forwarded by the Mac relay; the reply goes back out through the same SMS queue."""
    from duo_monitor import web
    if not web.s.sms_pull_token or not secrets.compare_digest(request.headers.get("x-token", ""), web.s.sms_pull_token):
        raise HTTPException(401, "bad token")
    phone = _phone(body.phone)
    # The relay reads the operator's personal Messages: a friend texting "yes" / "stop" is not a customer, never reply.
    if phone is None or not web.conn.execute("SELECT 1 FROM subscriptions WHERE phone=? LIMIT 1", (phone,)).fetchone():
        return {"action": "ignored"}
    action, reply = handle_command(web.conn, phone, body.text)
    if reply:
        db.enqueue_sms(web.conn, phone, reply)
    return {"action": action}


def twilio_signature(token: str, url: str, params: list[tuple[str, str]]) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + every POST key+value sorted by key))."""
    data = url + "".join(k + v for k, v in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@router.post("/twilio/inbound")
async def twilio_inbound(request: Request) -> Response:
    from duo_monitor import web
    token = web.s.twilio_token
    params = parse_qsl((await request.body()).decode(), keep_blank_values=True)
    query = f"?{request.url.query}" if request.url.query else ""
    # Behind Render's proxy request.url can say http://; Twilio signed the public URL, i.e. BASE_URL + path.
    urls = {str(request.url), web.s.base_url.rstrip("/") + request.url.path + query}
    got = request.headers.get("x-twilio-signature", "")
    if not token or not any(hmac.compare_digest(twilio_signature(token, u, params), got) for u in urls):
        raise HTTPException(403, "bad signature")
    form = dict(params)
    phone = _phone(form.get("From", ""))
    action, reply = handle_command(web.conn, phone, form.get("Body", "")) if phone else ("ignored", "")
    # STOP: Twilio Advanced Opt-Out already sends its own confirmation.
    xml = f"<Response><Message>{escape(reply)}</Message></Response>" if reply and action != "stop" else "<Response/>"
    return Response('<?xml version="1.0" encoding="UTF-8"?>' + xml, media_type="application/xml")


@router.post("/textbelt/inbound")
async def textbelt_inbound(request: Request) -> dict:
    """Replies to our Textbelt texts. Signed: X-textbelt-signature = HMAC-SHA256(key, timestamp + raw body)."""
    from duo_monitor import web
    key, raw = web.s.textbelt_key, (await request.body()).decode()
    ts, got = request.headers.get("x-textbelt-timestamp", ""), request.headers.get("x-textbelt-signature", "")
    want = hmac.new(key.encode(), (ts + raw).encode(), hashlib.sha256).hexdigest()
    if not key or not ts.isdigit() or abs(time.time() - int(ts)) > 900 or not hmac.compare_digest(want, got):
        raise HTTPException(403, "bad signature")
    body = json.loads(raw)
    phone = _phone(str(body.get("fromNumber", "")))
    action, reply = handle_command(web.conn, phone, str(body.get("text", ""))) if phone else ("ignored", "")
    sent = ""
    if reply and action != "stop":          # STOP: Textbelt blocks the number and confirms on its own
        sent = web.Notifier(web.s, web.conn).sms(phone, reply)[1]
    if phone:   # the customer's words are not stored, only what we did with them
        hooks.emit("sms.inbound", web.conn, sub_id=None, last4=phone[-4:], action=action, reply=sent)
    return {"action": action}
