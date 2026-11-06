"""Customer-facing copy in every language: duo_monitor/locales/<lang>.json, flat {key: text with {placeholders}}.

English is the source and the fallback for any key a language does not have yet. The server picks the language
(?lang= → `gdn_lang` cookie → Accept-Language → en) and pages receive only the keys they use (`bundle`).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).with_name("locales")
# code → name in that language (the language menu shows these)
LANGS = {"en": "English", "zh": "简体中文", "zh-Hant": "繁體中文", "es": "Español", "vi": "Tiếng Việt",
         "ko": "한국어", "ja": "日本語", "ar": "العربية", "fa": "فارسی"}
RTL = {"ar", "fa"}
HTML_LANG = {"zh": "zh-Hans", "zh-Hant": "zh-Hant"}
STRIPE_LOCALE = {"en": "en", "zh": "zh", "zh-Hant": "zh-TW", "es": "es", "vi": "vi", "ko": "ko", "ja": "ja"}  # else auto
COOKIE = "gdn_lang"


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    path = DIR / f"{lang}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def norm(lang: str | None) -> str:
    """A supported language code; accepts loose spellings (zh-CN, zh-TW, es-MX, KO…)."""
    raw = (lang or "").strip()
    if raw in LANGS:
        return raw
    low = raw.lower().replace("_", "-")
    if low.startswith("zh"):
        return "zh-Hant" if any(x in low for x in ("hant", "-tw", "-hk", "-mo")) else "zh"
    base = low.split("-")[0]
    return base if base in LANGS else "en"


def from_accept_language(header: str) -> str | None:
    """Best supported language from an Accept-Language header (order of preference; q-values ignored)."""
    for part in (header or "").split(","):
        tag = part.split(";")[0].strip()
        if tag and tag != "*" and norm(tag) != "en" or tag.lower().startswith("en"):
            return norm(tag)
    return None


def pick(request, default: str | None = None) -> str:
    """?lang= beats the saved cookie, which beats `default` (e.g. the subscription's language), then the browser."""
    q = request.query_params.get("lang")
    if q:
        return norm(q)
    if request.cookies.get(COOKIE):
        return norm(request.cookies.get(COOKIE))
    if default:
        return norm(default)
    return from_accept_language(request.headers.get("accept-language", "")) or "en"


def t(key: str, lang: str, **kw) -> str:
    """`<key>.one` is the singular form (n == 1) in languages that have one; never borrowed from English."""
    own = catalog(norm(lang))
    if kw.get("n") == 1 and f"{key}.one" in own:
        key = f"{key}.one"
    text = own.get(key) or catalog("en").get(key, key)
    return text.format(**kw) if kw else text


def bundle(lang: str, prefixes: tuple[str, ...]) -> dict[str, str]:
    """English merged with `lang` for every key under `prefixes` (what one page needs)."""
    en, own = catalog("en"), catalog(norm(lang))
    return {k: own.get(k) or v for k, v in en.items()
            if k.startswith(prefixes) and (k in own or not k.endswith(".one"))}


def page_head(lang: str, prefixes: tuple[str, ...]) -> dict[str, str]:
    """Template substitutions shared by the static pages."""
    lang = norm(lang)
    return {"__LANG__": HTML_LANG.get(lang, lang), "__DIR__": "rtl" if lang in RTL else "ltr",
            "__I18N__": json.dumps({"lang": lang, "langs": LANGS, "t": bundle(lang, prefixes)}, ensure_ascii=False)
            .replace("</", "<\\/")}


def switcher(lang: str, path: str) -> str:
    """Language menu for server-rendered pages: picking one reloads `path` with ?lang= (which also saves the cookie)."""
    from html import escape
    opts = "".join(f'<option value="{escape(path)}?lang={k}"{" selected" if k == lang else ""}>{v}</option>'
                   for k, v in LANGS.items())
    return (f'<select class="langsel" aria-label="{escape(t("common.language", lang))}" '
            f'onchange="location.href=this.value">{opts}</select>')


def html_attrs(lang: str) -> str:
    """lang + dir attributes for <html>."""
    lang = norm(lang)
    return f'lang="{HTML_LANG.get(lang, lang)}" dir="{"rtl" if lang in RTL else "ltr"}"'


def date(ts: str | None, lang: str) -> str:
    """A day in the language's own format (common.date: {y} {m} {d} {mon})."""
    if not ts:
        return ""
    from datetime import datetime
    d = datetime.fromisoformat(ts)
    return t("common.date", lang, y=d.year, m=d.month, d=d.day, mon=f"{d:%b}")
