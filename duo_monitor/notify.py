"""Alert delivery to a subscription's phone (SMS) and email. Unconfigured channels log to the console.

SMS backends:  Twilio (own verified toll-free number) · or Textbelt (`TEXTBELT_KEY`, shared sender pool) · or `SMS_OUTBOX=<path>`: append jobs to a local JSONL outbox that an external
sender drains · or `SMS_PULL_TOKEN`: queue in the DB for a relay to pull over HTTPS (`GET /api/sms-queue`,
see scripts/sms_relay.py). The last two are interim, while Twilio A2P registration is pending.
Email backends: Resend (production) · or SMTP (`SMTP_HOST` …).

Every send returns (ok, detail); detail says what really happened, always starting with one of:
  "sent:"     handed to the provider (Twilio / Resend / SMTP accepted it)
  "queued:"   written to the interim outbox / relay queue — delivered later by the relay, not by us
  "console:"  no backend configured (local dev): only logged, nobody received it — ok stays True
  "failed:"   provider error or exception
  "skipped:"  deliberately not sent (e.g. the phone replied STOP)
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage

import httpx

from .config import Settings

log = logging.getLogger("duo_monitor.notify")
_LINK = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")   # Textbelt rejects both until the key is whitelisted


class Notifier:
    def __init__(self, s: Settings, conn=None):
        self.s = s
        self.conn = conn          # needed only for the SMS pull queue

    def alert(self, sub, text: str, subject: str, kind: str | None = None, sms: str | None = None,
              email_only: bool = False) -> tuple[bool, str]:
        """Send to every contact on the subscription; ok if at least one channel delivered.
        kind ("alert" | "welcome") appends each channel's own feature footers (hooks.footer_text); None sends `text` as is.
        sms: a shorter text-message version of `text` (one SMS segment where possible); defaults to `text`.
        email_only: anything that is not a restock alert or the opt-in confirmation (follow-ups, referral notices,
        extensions) — SMS consent covers restock alerts only."""
        from .hooks import email_headers, footer_text

        def with_footer(channel: str, body: str) -> str:
            return body + footer_text(sub, kind, sub["lang"], channel) if kind else body
        results = []
        # restock texts only after the number replied YES; the welcome is the text that asks for that YES
        if sub["phone"] and not email_only and (kind != "alert" or sub["sms_ok_at"]):
            sms_text = with_footer("sms", sms or text)
            results.append(self._guard("sms", lambda: self.sms(sub["phone"], sms_text)))
        if sub["email"]:
            email_text = with_footer("email", text)
            results.append(self._guard("email", lambda: self.email(sub["email"], subject, email_text, email_headers(sub))))
        return any(ok for ok, _ in results), "; ".join(d for _, d in results)

    @staticmethod
    def _guard(name, fn) -> tuple[bool, str]:
        try:
            return fn()
        except (httpx.HTTPError, OSError, smtplib.SMTPException) as e:
            return False, f"failed: {name} {e}"

    @staticmethod
    def _http(what: str, r: httpx.Response) -> tuple[bool, str]:
        ok = r.status_code < 300
        return ok, f"{'sent' if ok else 'failed'}: {what} {r.status_code}"

    def _opted_out(self, phone: str) -> bool:
        """Phone replied STOP (features/optout.py records it): never text it again, whatever feature is sending."""
        if self.conn is None:
            return False
        try:
            row = self.conn.execute("SELECT status FROM sms_consent WHERE phone=?", (phone,)).fetchone()
        except Exception:  # noqa: BLE001 — table absent when the optout feature isn't loaded
            return False
        return bool(row) and row["status"] == "stopped"

    def sms(self, to: str, text: str) -> tuple[bool, str]:
        s = self.s
        if not s.sms_enabled:
            return False, "skipped: sms disabled (no business number yet)"
        if self._opted_out(to):
            return False, "skipped: sms opted out (STOP)"
        if s.twilio_ready:
            r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_sid}/Messages.json",
                           auth=(s.twilio_sid, s.twilio_token), data={"To": to, "From": s.twilio_from, "Body": text}, timeout=20)
            return self._http("sms twilio", r)
        if s.textbelt_key:
            return self._textbelt(to, text)
        if s.sms_outbox:
            with open(s.sms_outbox, "a") as f:
                f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "text": text, "to": [[to, "SMS"]]}) + "\n")
            return True, "queued: sms outbox"
        if s.sms_pull_token and self.conn is not None:
            from . import db
            db.enqueue_sms(self.conn, to, text)
            return True, "queued: sms queue"
        log.info("[console sms → %s] %s", to, text)
        return True, "console: sms (not delivered)"

    def _textbelt(self, to: str, text: str) -> tuple[bool, str]:
        """Textbelt handles STOP/START itself; replies (HELP) come back signed to /textbelt/inbound.
        Links and email addresses need Textbelt to whitelist the key; until then resend pointing to the email instead."""
        ok, detail = self._textbelt_post(to, text)
        if not ok and "URL" in detail and _LINK.search(text):
            stand_in = "（见邮件）" if re.search(r"[\u4e00-\u9fff]", text) else "(see your email)"
            ok, detail = self._textbelt_post(to, _LINK.sub(stand_in, text))
            detail += " (link removed: key not whitelisted yet)"
        return ok, detail

    def _textbelt_post(self, to: str, text: str) -> tuple[bool, str]:
        r = httpx.post("https://textbelt.com/text", data={
            "phone": to[2:] if to.startswith("+1") else to, "message": text, "key": self.s.textbelt_key,
            "sender": "GetDuoNow", "replyWebhookUrl": f"{self.s.base_url}/textbelt/inbound"}, timeout=20)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if body.get("success"):
            return True, f"sent: sms textbelt {body.get('textId', '')}".rstrip()   # textId: delivery status lookup
        error = re.sub(r"key=\S+", "key=…", str(body.get("error") or r.status_code))   # errors echo the key
        return False, f"failed: sms textbelt {error}"

    def email(self, to: str, subject: str, text: str, headers: dict | None = None) -> tuple[bool, str]:
        s = self.s
        headers = headers or {}
        if s.resend_ready:
            payload = {"from": s.email_from, "to": [to], "subject": subject, "text": text, "headers": headers}
            if s.reply_to:
                payload["reply_to"] = s.reply_to
            r = httpx.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {s.resend_key}"},
                           json=payload, timeout=20)
            return self._http("email resend", r)
        if s.smtp_ready:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = s.email_from or s.smtp_user, to, subject
            if s.reply_to:
                msg["Reply-To"] = s.reply_to
            for k, v in headers.items():
                msg[k] = v
            msg.set_content(text)
            with smtplib.SMTP_SSL(s.smtp_host, 465, timeout=20) as m:
                m.login(s.smtp_user, s.smtp_password)
                m.send_message(msg)
            return True, "sent: email smtp"
        log.info("[console email → %s] %s | %s", to, subject, text)
        return True, "console: email (not delivered)"
