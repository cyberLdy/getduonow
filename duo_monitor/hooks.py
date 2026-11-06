"""Extension points: feature modules in duo_monitor/features/ plug in here instead of editing core files.

Events (emitted by core code; handlers get the DB connection plus keyword data):
  subscription.created    sub_id, ref            — form submitted (pending until paid / comped)
  subscription.activated  sub_id, via            — via: "stripe" | "free" | "admin"
  alert.sent              sub_id, store, part, ok, detail
  subscriptions.expired   count
  cell.soldout            store, part, store_name, since, minutes   — a watched cell went from in stock to out of stock
  poll.pass               stats                  — end of every poller pass (~30 s): use for periodic jobs
Message footers: functions (sub, kind, lang) -> str appended to customer messages; kind: "alert" | "welcome".
                 Notifier.alert appends them per channel. `@footer` = email only (links cost SMS segments and SMS
                 opts out by "Reply STOP"); `@footer(channels=("email", "sms"))` also puts it in texts.
Email headers:   functions (sub) -> dict merged into alert/welcome email headers (e.g. List-Unsubscribe).
A failing handler is logged and skipped: features must never break polling, payments or alerts.
"""
from __future__ import annotations

import logging

log = logging.getLogger("duo_monitor.hooks")
_handlers: dict[str, list] = {}
_footers: list = []
_email_headers: list = []


def on(event: str):
    def register(fn):
        _handlers.setdefault(event, []).append(fn)
        return fn
    return register


def emit(event: str, conn, **data) -> None:
    for fn in _handlers.get(event, []):
        try:
            fn(conn, **data)
        except Exception:  # noqa: BLE001
            log.exception("hook %s failed in %s", event, getattr(fn, "__name__", fn))


def footer(fn=None, *, channels: tuple[str, ...] = ("email",)):
    def register(f):
        _footers.append((f, tuple(channels)))
        return f
    return register(fn) if fn is not None else register


def email_header(fn):
    _email_headers.append(fn)
    return fn


def footer_text(sub, kind: str, lang: str, channel: str = "email") -> str:
    parts = []
    for fn, channels in _footers:
        if channel not in channels:
            continue
        try:
            text = fn(sub, kind, lang)
        except Exception:  # noqa: BLE001
            log.exception("footer %s failed", getattr(fn, "__name__", fn))
            continue
        if text:
            parts.append(text)
    return ("\n" + "\n".join(parts)) if parts else ""


def email_headers(sub) -> dict:
    headers: dict = {}
    for fn in _email_headers:
        try:
            headers.update(fn(sub) or {})
        except Exception:  # noqa: BLE001
            log.exception("email header %s failed", getattr(fn, "__name__", fn))
    return headers
