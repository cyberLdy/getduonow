"""Settings from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


# Pickup stock is per hardware part; the carrier only decides which version the customer buys at checkout
# (verified 10-31: `cppart` does not change pickup-message results).
CARRIERS = {"unlocked": "Unlocked", "att": "AT&T", "tmobile": "T-Mobile", "verizon": "Verizon"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "duo_monitor.db"))
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000"))
    poll_seconds: int = field(default_factory=lambda: int(_env("POLL_SECONDS", "30")))
    # comma-separated buy-page slugs offered in the form, e.g. iphone-duo,iphone-17
    families: tuple[str, ...] = field(default_factory=lambda: tuple(
        f for f in _env("FAMILIES", "iphone-duo").split(",") if f))
    proxies: tuple[str, ...] = field(default_factory=lambda: tuple(p for p in _env("PROXIES").split(",") if p))
    # = every store Apple returns for one ZIP (12): dense metros (NYC, LA) have that many in reach. Load does not grow with
    # it — the poller asks once per distinct store for everyone watching it.
    max_stores: int = field(default_factory=lambda: int(_env("MAX_STORES", "12")))
    watch_days: int = field(default_factory=lambda: int(_env("WATCH_DAYS", "14")))
    price_cents: int = field(default_factory=lambda: int(_env("PRICE_CENTS", "499")))

    basic_auth: str = field(default_factory=lambda: _env("BASIC_AUTH"))   # "user:pass" gates the whole site (pre-launch)
    access_key: str = field(default_factory=lambda: _env("ACCESS_KEY"))   # share link /?key=… — no login prompt (pre-launch)
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN"))   # X-Admin-Token: comp subscriptions, expire
    stripe_key: str = field(default_factory=lambda: _env("STRIPE_SECRET_KEY"))
    stripe_webhook_secret: str = field(default_factory=lambda: _env("STRIPE_WEBHOOK_SECRET"))

    twilio_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_from: str = field(default_factory=lambda: _env("TWILIO_FROM"))
    # Textbelt: shared business sender pool, no number registration; used until our own Twilio number is verified.
    textbelt_key: str = field(default_factory=lambda: _env("TEXTBELT_KEY"))
    # Customer texts only from a business number (Twilio). Never relay through the operator's own phone/Apple ID:
    # that exposes the owner's personal number. SMS_ENABLED=1 turns texts on once Twilio is live.
    sms_enabled: bool = field(default_factory=lambda: _env("SMS_ENABLED", "0") == "1")
    # The form's SMS opt-in (unticked box + number). SMS_SIGNUP=1 opens it before sending goes live, because
    # toll-free verification reviews the live opt-in form; texts still wait for SMS_ENABLED.
    sms_signup: bool = field(default_factory=lambda: "1" in (_env("SMS_SIGNUP", "0"), _env("SMS_ENABLED", "0")))
    sms_outbox: str = field(default_factory=lambda: _env("SMS_OUTBOX"))   # interim: JSONL drained by an external sender
    sms_pull_token: str = field(default_factory=lambda: _env("SMS_PULL_TOKEN"))   # interim: relay pulls /api/sms-queue
    run_poller: bool = field(default_factory=lambda: _env("RUN_POLLER") == "1")  # poll inside the web process
    resend_key: str = field(default_factory=lambda: _env("RESEND_API_KEY"))
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM"))
    # The sending domain has no mailbox: customers' replies (refunds, help) go to this monitored inbox.
    reply_to: str = field(default_factory=lambda: _env("REPLY_TO"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))

    @property
    def payments_on(self) -> bool:
        return bool(self.stripe_key)

    @property
    def twilio_ready(self) -> bool:
        return bool(self.twilio_sid and self.twilio_token and self.twilio_from)

    @property
    def resend_ready(self) -> bool:
        return bool(self.resend_key and self.email_from)

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)
