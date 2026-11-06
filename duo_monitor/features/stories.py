"""Success stories (FTC-honest): customers tell us whether they got their phone; only consented stories go public.

Alert footer "Got one? Tell us: <signed /got link>" → /got form → success_stories → /stories page + /api/stories/summary.
Nothing here is ever written by us: the count and every story come from customers' own submissions.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from duo_monitor import hooks, i18n, signing

router = APIRouter()
PURPOSE = "got"
QUOTE_MAX = 140
SUMMARY_N = 6
# Real order screenshots, personal details blurred before upload: [{"src": "/static/proof/x.webp", "en": "...", "zh": "..."}]
PROOF = Path(__file__).resolve().parents[1] / "static" / "proof" / "proof.json"
PAGE_N = 100
US_DAY = timezone(timedelta(hours=-8))   # date stories in US time: a 7 pm report must not show tomorrow's (UTC) date

SCHEMA = """
CREATE TABLE IF NOT EXISTS success_stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id TEXT NOT NULL UNIQUE,
    got INTEGER NOT NULL,              -- 1 = got the phone, 0 = not yet
    name TEXT,
    city TEXT,
    quote TEXT,
    consent INTEGER NOT NULL DEFAULT 0, -- 1 = OK to show initial + city + quote publicly
    product TEXT,
    store_name TEXT,
    created_at TEXT NOT NULL           -- time of the latest answer (a resubmission moves it)
);
"""


def setup(conn) -> None:
    conn.executescript(SCHEMA)


def _web():
    from duo_monitor import web   # lazy: web imports the features package
    return web


@hooks.footer
def got_footer(sub, kind: str, lang: str) -> str:
    if kind != "alert":
        return ""
    url = signing.link(_web().s.base_url, PURPOSE, sub["id"])
    return i18n.t("stories.gotFooter", lang, url=url)


# ---------- helpers ----------

def _sub_or_404(sub_id: str, sig: str):
    web = _web()
    sub = web.db.get_subscription(web.conn, sub_id) if signing.verify(sub_id, PURPOSE, sig) else None
    if sub is None:
        raise HTTPException(404, "Not found")
    return sub


def _clean(raw: str, limit: int) -> str:
    """Plain one-line text: no tags, no control chars, collapsed spaces, capped length."""
    text = re.sub(r"[<>\x00-\x1f\x7f]", " ", raw or "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _what(conn, sub_id: str) -> tuple[str | None, str | None]:
    """(product, store_name) of the subscription's latest delivered alert, else of its first watch."""
    row = conn.execute(
        "SELECT w.product, w.store_name FROM alerts a JOIN watches w ON w.sub_id = a.sub_id AND w.store = a.store "
        "AND w.part = a.part WHERE a.sub_id = ? AND a.ok = 1 ORDER BY a.sent_at DESC, a.id DESC LIMIT 1",
        (sub_id,)).fetchone()
    if row is None:
        first = _web().db.get_watches(conn, sub_id)
        row = first[0] if first else None
    return (row["product"], row["store_name"]) if row else (None, None)


def _public(conn, limit: int) -> list[dict]:
    rows = conn.execute("SELECT name, city, quote, product, store_name, created_at FROM success_stories "
                        "WHERE got = 1 AND consent = 1 ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        initial = next((c.upper() + "." for c in (r["name"] or "") if c.isalpha()), "")
        day = datetime.fromisoformat(r["created_at"]).astimezone(US_DAY)
        out.append({"initial": initial, "city": r["city"] or "", "quote": r["quote"] or "",
                    "product": r["product"] or "", "store_name": r["store_name"] or "", "date": f"{day:%Y-%m-%d}"})
    return out


def _got_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM success_stories WHERE got = 1").fetchone()[0]


CSS = """
:root{--bg:#f5f5f2;--card:rgba(255,255,255,.78);--ink:#1d1d1f;--mute:#6e6e73;--faint:#a1a1a6;--line:#e3e2dd;--ok:#2f7a55;--sel:#1d1d1f;--sel-ink:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f17;--card:rgba(255,255,255,.06);--ink:#f4f6fa;--mute:#a3a8b3;--faint:#6b7180;--line:rgba(255,255,255,.12);--ok:#9fdcb9;--sel:#f4f6fa;--sel-ink:#0b0f17}}
*{box-sizing:border-box}
.langsel{font:inherit;font-size:14px;color:inherit;background:transparent;border:1px solid var(--line);border-radius:999px;padding:6px 10px;max-width:150px}:lang(ko){word-break:keep-all;overflow-wrap:anywhere}[dir=rtl] h2{text-transform:none;letter-spacing:0}
.proof{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:0 0 14px}.proof figure{margin:0}.proof img{width:100%;display:block;border-radius:14px;border:1px solid var(--line)}.proof figcaption{font-size:12.5px;color:var(--mute);margin-top:4px}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","PingFang SC",sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:620px;margin:0 auto;padding:20px 16px 72px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
.brand{font-weight:700;letter-spacing:-.02em;font-size:19px;color:inherit;text-decoration:none}
.lang a{color:var(--mute);font-size:14px;margin-left:12px}
h1{font-size:34px;line-height:1.08;letter-spacing:-.03em;margin:0 0 12px}
.sub{color:var(--mute);margin:0 0 22px;font-size:17px}
.card{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:18px;margin-bottom:12px}
.row{display:flex;gap:8px}
.btn{flex:1;border:1px solid var(--sel);border-radius:12px;padding:13px 16px;background:var(--sel);color:var(--sel-ink);font:inherit;font-weight:600;cursor:pointer;text-align:center}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
#yes{position:absolute;opacity:0}
#yes:checked ~ .row label{outline:2px solid var(--ok);outline-offset:2px}
.more{display:none;margin-top:14px}
#yes:checked ~ .more{display:block}
.more > * + *{margin-top:10px}
input[type=text],textarea{width:100%;font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:12px;padding:12px 13px}
textarea{resize:vertical;min-height:84px}
.check{display:flex;gap:10px;align-items:flex-start;font-size:15px;color:var(--mute)}
.check input{margin-top:4px}
.big{font-size:22px;font-weight:700;letter-spacing:-.02em}
.story + .story{border-top:1px solid var(--line);margin-top:14px;padding-top:14px}
.who{font-weight:600}.meta{color:var(--mute);font-size:14px}
blockquote{margin:6px 0 0;color:var(--ink)}
.fine{color:var(--faint);font-size:12.5px;margin-top:14px}
"""


def _page(lang: str, title: str, body: str, path: str) -> HTMLResponse:
    fine = html.escape(i18n.t("stories.fine", lang))
    return HTMLResponse(
        f'<!doctype html><html {i18n.html_attrs(lang)}><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · GetDuoNow</title>'
        f'<link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><style>{CSS}</style></head><body><main>'
        f'<div class="top"><a class="brand" href="/">GetDuoNow</a>{i18n.switcher(lang, path)}</div>'
        f'{body}<p class="fine">{fine}</p></main></body></html>')


@router.get("/got/{sub_id}/{sig}", response_class=HTMLResponse)
def got_form(sub_id: str, sig: str, request: Request) -> HTMLResponse:
    sub = _sub_or_404(sub_id, sig)
    lang = i18n.pick(request, sub["lang"])
    t = {k: html.escape(i18n.t(f"stories.{k}", lang, max=QUOTE_MAX)) for k in
         ("formH", "formSub", "yes", "no", "name", "city", "quote", "consent", "send")}
    t["h"], t["sub"] = t["formH"], t["formSub"]
    body = (
        f'<h1>{t["h"]}</h1><p class="sub">{t["sub"]}</p>'
        f'<form id="notyet" method="post"><input type="hidden" name="got" value="0">'
        f'<input type="hidden" name="lang" value="{lang}"></form>'
        f'<form class="card" method="post"><input type="hidden" name="got" value="1">'
        f'<input type="hidden" name="lang" value="{lang}">'
        f'<input type="checkbox" id="yes" aria-controls="more">'
        f'<div class="row"><label class="btn" for="yes">{t["yes"]}</label>'
        f'<button class="btn ghost" type="submit" form="notyet">{t["no"]}</button></div>'
        f'<div class="more" id="more">'
        f'<input type="text" name="name" maxlength="40" autocomplete="given-name" placeholder="{t["name"]}" aria-label="{t["name"]}">'
        f'<input type="text" name="city" maxlength="60" autocomplete="address-level2" placeholder="{t["city"]}" aria-label="{t["city"]}">'
        f'<textarea name="quote" maxlength="{QUOTE_MAX}" placeholder="{t["quote"]}" aria-label="{t["quote"]}"></textarea>'
        f'<label class="check"><input type="checkbox" name="consent" value="1"><span>{t["consent"]}</span></label>'
        f'<button class="btn" type="submit" style="width:100%">{t["send"]}</button></div></form>')
    return _page(lang, t["h"], body, request.url.path)


@router.post("/got/{sub_id}/{sig}", response_class=HTMLResponse)
async def got_save(sub_id: str, sig: str, request: Request) -> HTMLResponse:
    sub = _sub_or_404(sub_id, sig)
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    got = form.get("got") == "1"
    name, city, quote = (_clean(form.get("name", ""), 40), _clean(form.get("city", ""), 60),
                         _clean(form.get("quote", ""), QUOTE_MAX)) if got else ("", "", "")
    consent = int(got and form.get("consent") in ("1", "on"))
    web = _web()
    product, store_name = _what(web.conn, sub_id)
    with web.conn:
        web.conn.execute(
            "INSERT INTO success_stories (sub_id, got, name, city, quote, consent, product, store_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sub_id) DO UPDATE SET got=excluded.got, name=excluded.name, "
            "city=excluded.city, quote=excluded.quote, consent=excluded.consent, product=excluded.product, "
            "store_name=excluded.store_name, created_at=excluded.created_at",
            (sub_id, int(got), name or None, city or None, quote or None, consent, product, store_name,
             web.db.iso(web.db.now())))
    lang = i18n.norm(form["lang"]) if form.get("lang") else i18n.pick(request, sub["lang"])
    if got:
        h, p = html.escape(i18n.t("stories.congratsH", lang)), html.escape(i18n.t("stories.congratsP", lang))
        if consent:
            link = f'<a href="/stories?lang={lang}">{html.escape(i18n.t("stories.pageLink", lang))}</a>'
            p += " " + html.escape(i18n.t("stories.congratsPublic", lang)).replace("{link}", link)
    else:
        h, p = html.escape(i18n.t("stories.thanksH", lang)), html.escape(i18n.t("stories.thanksP", lang))
    return _page(lang, h, f'<h1>{h}</h1><p class="sub">{p}</p>', f"/got/{sub_id}/{sig}")


def _proof_items() -> list[dict]:
    try:
        return json.loads(PROOF.read_text())
    except (OSError, ValueError):
        return []


def _proof(lang: str) -> str:
    figs = "".join(f'<figure><img src="{html.escape(i["src"])}" alt="" loading="lazy">'
                   f'<figcaption>{html.escape(i.get(lang) or i.get("en", ""))}</figcaption></figure>' for i in _proof_items())
    return f'<h2>{html.escape(i18n.t("stories.proofH", lang))}</h2><div class="proof">{figs}</div>' if figs else ""


@router.get("/stories", response_class=HTMLResponse)
def stories_page(request: Request) -> HTMLResponse:
    lang = i18n.pick(request)
    conn = _web().conn
    n = _got_count(conn)
    items = []
    for st in _public(conn, PAGE_N):
        who = ", ".join(x for x in (st["initial"], st["city"]) if x)
        what = i18n.t("stories.what", lang, product=st["product"], store=st["store_name"]) if st["store_name"] else st["product"]
        line = " — ".join(html.escape(x) for x in (who, what, i18n.date(st["date"], lang)) if x)
        quote = f'<blockquote>“{html.escape(st["quote"])}”</blockquote>' if st["quote"] else ""
        items.append(f'<div class="story"><div class="who">{line}</div>{quote}</div>')
    h = html.escape(i18n.t("stories.h", lang))
    if n:
        count = html.escape(i18n.t("stories.count1" if n == 1 else "stories.count", lang, n=n))
        head = f'<div class="card"><div class="big">{count}</div></div>'
    else:
        head = ""
    proof = _proof(lang)
    if items:
        lst = f'<div class="card">{"".join(items)}</div>'
    elif proof:
        lst = ""
    else:
        lst = ('<div class="card"><p class="meta" style="margin:0">'
               + html.escape(i18n.t("stories.empty", lang)) + "</p></div>")
    sub = html.escape(i18n.t("stories.sub", lang))
    return _page(lang, h, f'<h1>{h}</h1><p class="sub">{sub}</p>{proof}{head}{lst}', request.url.path)


@router.get("/api/stories/summary")
def stories_summary() -> dict:
    """Plain text values (not HTML-escaped): render with textContent, never innerHTML."""
    conn = _web().conn
    return {"got": _got_count(conn), "stories": _public(conn, SUMMARY_N), "proof": len(_proof_items())}
