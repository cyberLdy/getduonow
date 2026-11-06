"""Customer-facing message copy, in the subscription's language (duo_monitor/locales/<lang>.json, English fallback).
Every message carries the brand and an opt-out line."""
from __future__ import annotations

import os

from . import i18n

BRAND = "GetDuoNow"


def _carrier(carrier: str, lang: str) -> str:
    return i18n.t(f"web.carrier.{carrier}", lang) if f"web.carrier.{carrier}" in i18n.catalog("en") else carrier


def order_url(part: str) -> str:
    """Apple redirects /shop/product/<part> to that model's buy page, for any iPhone family."""
    return f"https://www.apple.com/shop/product/{part}"


def alert(product: str, carrier: str, store_name: str, city: str, quote: str, part: str, lang: str) -> tuple[str, str]:
    """→ (subject, body) for an in-stock alert."""
    return (i18n.t("msg.alertSubject", lang, product=product, store=store_name),
            i18n.t("msg.alertBody", lang, product=product, carrier=_carrier(carrier, lang), store=store_name, city=city,
                   quote=quote, url=order_url(part)))


def alert_sms(product: str, store_name: str, part: str, lang: str) -> str:
    """Text-message version of `alert`: aims for one SMS segment (≤160 GSM chars in English); no footers/links but the order URL."""
    return i18n.t("msg.alertSms", lang, product=product, store=store_name, url=order_url(part))


def welcome_sms(lang: str) -> str:
    """Text-message opt-in confirmation (one segment): brand, frequency, rates, HELP/STOP — what carriers require."""
    return i18n.t("msg.welcomeSms", lang)


def _brief(names: list[str], keep: int = 2) -> str:
    """First `keep` names, then +N — a text message stays short however many models/stores are watched."""
    names = list(dict.fromkeys(names))
    return ", ".join(names[:keep]) + (f" +{len(names) - keep}" if len(names) > keep else "")


def _what_where(watches: list) -> dict:
    models = list(dict.fromkeys(w["product"] for w in watches))
    shared = os.path.commonprefix(models)[:os.path.commonprefix(models).rfind(" ") + 1] if len(models) > 1 else ""
    if shared:    # "iPhone Duo 256GB Night Sky, 512GB Star White": the shared words once
        models = [models[0]] + [m[len(shared):] for m in models[1:]]
    return {"models": _brief(models), "stores": _brief([w["store_name"] for w in watches])}


def confirm_sms(watches: list, lang: str) -> str:
    """Double opt-in: names the models and stores, asks for YES. Texts about restocks start only after it."""
    return i18n.t("msg.confirmSms", lang, **_what_where(watches))


def confirmed_sms(watches: list, lang: str) -> str:
    return i18n.t("msg.confirmedSms", lang, **_what_where(watches))


def welcome(watches: list, carrier: str, days: int, lang: str) -> tuple[str, str]:
    """Sent once when a watch starts: confirms the channel works and what we watch."""
    by_store: dict[str, list[str]] = {}
    for w in watches:
        by_store.setdefault(w["store_name"], []).append(w["product"])
    lines = "\n".join(f"- {store}: {', '.join(products)}" for store, products in by_store.items())
    return (i18n.t("msg.welcomeSubject", lang),
            i18n.t("msg.welcomeBody", lang, carrier=_carrier(carrier, lang), days=days, lines=lines))
