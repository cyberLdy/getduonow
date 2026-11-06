"""Web app: sign-up form, catalog / store lookup, Stripe Checkout, Stripe webhook.

    uvicorn duo_monitor.web:app --port 8000
"""
from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, hooks, i18n, messages, poller, signing
from .apple import AppleClient
from .config import CARRIERS, Settings
from .notify import Notifier
from .ratelimit import RateLimit, client_ip

s = Settings()
conn = db.connect(s.db_path)
client = AppleClient(list(s.proxies) or None)
# Store lookups hit Apple from the poller's own IP: a flood there would get the stock checks blocked.
STORE_LOOKUPS = RateLimit(20, 60)       # per visitor
SIGNUPS = RateLimit(10, 600)            # per visitor
APPLE_BUDGET = RateLimit(120, 60)       # all visitors together: lookups that actually reach Apple
STORE_CACHE_S = 60
_store_cache: dict[tuple[str, str], tuple[float, list]] = {}
app = FastAPI(title="GetDuoNow")
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
PAGES = Path(__file__).with_name("templates")
from .features import load as _load_features  # noqa: E402

for _router in _load_features(conn):
    app.include_router(_router)

@app.middleware("http")
async def access_gate(request: Request, call_next):
    """Pre-launch gate. Free mode sends SMS from the operator's own number, so strangers must not reach the form.
    Ways in: a share link `/?key=<ACCESS_KEY>` (sets a cookie, then redirects to the clean URL), the cookie itself,
    or HTTP basic auth. The Stripe webhook stays open (Stripe authenticates with its signature)."""
    # Open even pre-launch: links customers tap from alerts carry their own signature (/unsub /extend /got), the relay
    # and webhooks authenticate with tokens/signatures, and stories + static assets are public by nature.
    if not (s.basic_auth or s.access_key) or request.url.path in (
            "/stripe/webhook", "/api/sms-queue", "/api/sms-inbound", "/healthz", "/stories", "/api/stories/summary") \
            or request.url.path.startswith(("/static/", "/twilio/", "/unsub/", "/extend/", "/got/")):
        return await call_next(request)
    if s.access_key and secrets.compare_digest(request.query_params.get("key", ""), s.access_key):
        resp = RedirectResponse(request.url.path, status_code=303)
        resp.set_cookie("gdn_key", s.access_key, max_age=30 * 86400, httponly=True, secure=True, samesite="lax")
        return resp
    if s.access_key and secrets.compare_digest(request.cookies.get("gdn_key", ""), s.access_key):
        return await call_next(request)
    if s.basic_auth and secrets.compare_digest(request.headers.get("authorization", ""),
                                               "Basic " + base64.b64encode(s.basic_auth.encode()).decode()):
        return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="GetDuoNow"'})


@app.on_event("startup")
def _start_poller() -> None:
    """One Render service = one disk: the poller runs as a thread next to the web app (RUN_POLLER=1)."""
    if s.run_poller:
        threading.Thread(target=poller.run_forever, args=(s,), kwargs={"client": client}, name="poller",
                         daemon=True).start()


STARTED_AT = time.time()
POLLER_STALE = 600      # seconds without a completed pass → 503 → Render restarts the instance. 10 min, not 3:
                        # a pass sends alerts synchronously (20 s timeouts), so only a truly stuck poller should trip it


@app.get("/healthz")
def healthz():
    """last_cycle_age = seconds since the poller's last completed pass (null before the first). With RUN_POLLER=1, a
    poller that is dead or stuck answers 503 so the platform restarts the instance."""
    now, last = time.time(), poller.last_pass_at
    st = poller.last_stats
    # apple: the last pass's requests vs errors. All failing = Apple is refusing us (e.g. blocked IP): the process is
    # healthy and a restart would not help, so this stays 200 and the watchdog (scripts/watchdog.py) emails the owner.
    out = {"ok": True, "last_cycle_age": round(now - last, 1) if last else None,
           "apple": {"requests": st.get("requests", 0), "errors": st.get("errors", 0),
                     "exits": len(s.proxies), "resting": client.resting()}}
    if s.run_poller and now - (last or STARTED_AT) > POLLER_STALE:
        return JSONResponse({**out, "ok": False}, status_code=503)
    return out


@app.get("/api/sms-queue")
def sms_queue(request: Request) -> list[dict]:
    """Interim SMS relay: hands pending texts to the operator's relay once (header X-Token: SMS_PULL_TOKEN)."""
    if not s.sms_pull_token or not secrets.compare_digest(request.headers.get("x-token", ""), s.sms_pull_token):
        raise HTTPException(401, "bad token")
    return db.pull_sms(conn)


_catalog: dict = {"at": 0.0, "items": []}
CATALOG_TTL = 3600


def _split(name: str) -> tuple[str, str]:
    """'iPhone Duo 256GB Star White' → ('256GB', 'Star White')."""
    m = re.search(r"(\d+\s*(?:GB|TB))\s+(.+)$", name)
    return (m.group(1).replace(" ", ""), m.group(2)) if m else ("", name)


def _gb(size: str) -> int:
    m = re.match(r"(\d+)(GB|TB)", size)
    return int(m.group(1)) * (1024 if m.group(2) == "TB" else 1) if m else 0


def catalog() -> list[dict]:
    """Every model of every family in FAMILIES (first family = the hero, shown first)."""
    if time.time() - _catalog["at"] > CATALOG_TTL or not _catalog["items"]:
        items = []
        for rank, fam in enumerate(s.families):
            try:
                for p in client.catalog(fam):
                    size, color = _split(p.name)
                    items.append({"family": fam, "family_name": p.name.split(size)[0].strip() if size else fam,
                                  "rank": rank, "part": p.part, "name": p.name, "size": size, "color": color, "price": p.price})
            except Exception:  # noqa: BLE001 — a renamed/removed family should not take the form down
                continue
        if items:
            _catalog.update(at=time.time(), items=sorted(items, key=lambda x: (x["rank"], _gb(x["size"]), x["color"])))
    return _catalog["items"]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise HTTPException(400, "Enter a US mobile number")
    return "+" + digits


def normalize_email(raw: str) -> str:
    e = raw.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise HTTPException(400, "Enter a valid email")
    return e


class StoreWatch(BaseModel):
    store: str
    store_name: str
    parts: list[str]


class SubscribeIn(BaseModel):
    email: str = ""
    phone: str = ""
    sms_opt_in: bool = False   # the form's own SMS consent box (unticked by default); without it the number is not kept
    carrier: str = "unlocked"
    lang: str = "en"
    ref: str = ""          # referral code from a share link (features/referral.py)
    stores: list[StoreWatch]


def _page(name: str, request: Request, prefixes: tuple[str, ...]) -> HTMLResponse:
    """A static page in the visitor's language (?lang= → cookie → browser); ?lang= is remembered in the cookie."""
    lang = i18n.pick(request)
    html = (PAGES / name).read_text().replace("__BASE__", s.base_url.rstrip("/"))   # absolute og:image for link previews
    for k, v in i18n.page_head(lang, prefixes).items():
        html = html.replace(k, v)
    resp = HTMLResponse(html)
    if request.query_params.get("lang"):
        resp.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _page("index.html", request, ("web.", "common."))


def _lang_link(code: str):
    return lambda: RedirectResponse(f"/?lang={code}", status_code=302)


for _code in i18n.LANGS:    # short share links per language: /zh, /es, /ko … (explicit routes: no catch-all shadowing)
    app.add_api_route(f"/{_code}", _lang_link(_code), methods=["GET"], include_in_schema=False)


@app.get("/done", response_class=HTMLResponse)
def done(request: Request) -> HTMLResponse:
    return _page("done.html", request, ("done.", "common."))


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request) -> HTMLResponse:
    return _page("terms.html", request, ("terms.", "common."))


@app.get("/api/config")
def config() -> dict:
    return {"payments": s.payments_on, "price_cents": s.price_cents, "max_stores": s.max_stores,
            "watch_days": s.watch_days, "carriers": CARRIERS, "sms": s.sms_enabled,
            "sms_signup": s.sms_signup, "support_email": s.reply_to}


@app.get("/api/products")
def products() -> list[dict]:
    return catalog()


@app.get("/api/stores")
def stores(location: str, request: Request, family: str = "") -> list[dict]:
    """Nearest stores + which models of `family` are in stock there right now (the store picker's live badges)."""
    location = location.strip()
    if not re.fullmatch(r"[\w ,.\-]{3,60}", location):
        raise HTTPException(400, "Enter a ZIP code or city")
    if not STORE_LOOKUPS.hit(client_ip(request)):
        raise HTTPException(429, "Too many lookups. Try again in a minute.")
    key = (location.lower(), family)
    cached = _store_cache.get(key)
    if cached and time.time() - cached[0] < STORE_CACHE_S:
        return cached[1]
    if not APPLE_BUDGET.hit("all"):
        raise HTTPException(429, "Busy right now. Try again in a minute.")
    result = _lookup_stores(location, family)
    if len(_store_cache) > 2000:
        _store_cache.clear()
    _store_cache[key] = (time.time(), result)
    return result


def _lookup_stores(location: str, family: str) -> list[dict]:
    items = catalog()
    if not items:
        raise HTTPException(503, "Model list unavailable, try again shortly")
    fam = family or items[0]["family"]
    parts = [p["part"] for p in items if p["family"] == fam] or [items[0]["part"]]
    try:
        found, cells = client.availability_near(location, parts)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Store lookup failed: {e}") from e
    stock: dict[str, dict[str, str]] = {}
    for c in cells:
        if c.available:
            stock.setdefault(c.store, {})[c.part] = c.quote
    return [{"number": x.number, "name": x.name, "city": x.city, "state": x.state, "miles": x.miles,
             "stock": stock.get(x.number, {})} for x in found]


def _validate(body: SubscribeIn) -> tuple[str | None, str | None, list[tuple[str, str, str, str]]]:
    # Texts are the primary channel (a restock lasts minutes); email is the backup. Either one is enough.
    email = normalize_email(body.email) if body.email.strip() else None
    phone = None
    if body.sms_opt_in and s.sms_signup and body.phone.strip():
        phone = normalize_phone(body.phone)
    if not email and not phone:
        raise HTTPException(400, "Enter your mobile number (and tick text alerts) or your email")
    if body.carrier not in CARRIERS:
        raise HTTPException(400, "Unknown carrier")
    names = {p["part"]: p["name"] for p in catalog()}
    picked = [w for w in body.stores if w.parts]
    if not 0 < len(picked) <= s.max_stores:
        raise HTTPException(400, f"Pick 1 to {s.max_stores} stores, each with at least one model")
    watches = []
    for w in picked:
        if not re.fullmatch(r"R\d{3}", w.store):
            raise HTTPException(400, "Unknown store")
        store_name = re.sub(r"[^\w .,'&-]", "", w.store_name)[:60] or w.store
        for part in dict.fromkeys(w.parts):
            if part not in names:
                raise HTTPException(400, "Unknown model")
            watches.append((w.store, store_name, part, names[part]))
    return email, phone, watches


def _welcome(sub_id: str) -> None:
    """Confirmation on activation (proves the channel works). Background thread: never slows the response/webhook."""
    def send() -> None:
        sub = db.get_subscription(conn, sub_id)
        watches = db.get_watches(conn, sub_id)
        subject, body = messages.welcome(watches, sub["carrier"], s.watch_days, sub["lang"])
        sms = messages.welcome_sms(sub["lang"]) if sub["sms_ok_at"] else messages.confirm_sms(watches, sub["lang"])
        ok, detail = Notifier(s, conn).alert(sub, body, subject, kind="welcome", sms=sms)
        hooks.emit("welcome.sent", conn, sub_id=sub_id, ok=ok, detail=detail, confirm=not sub["sms_ok_at"])
    threading.Thread(target=send, name="welcome", daemon=True).start()


def _is_admin(request: Request) -> bool:
    return bool(s.admin_token) and secrets.compare_digest(request.headers.get("x-admin-token", ""), s.admin_token)


@app.post("/api/subscribe")
def subscribe(body: SubscribeIn, request: Request) -> dict:
    if not _is_admin(request) and not SIGNUPS.hit(client_ip(request)):
        raise HTTPException(429, "Too many sign-ups from this network. Try again later.")
    email, phone, watches = _validate(body)
    lang = i18n.norm(body.lang)
    sub_id = db.create_subscription(conn, email=email, phone=phone, carrier=body.carrier, watches=watches, lang=lang)
    hooks.emit("subscription.created", conn, sub_id=sub_id, ref=body.ref.strip()[:40])
    if not s.payments_on or _is_admin(request):      # admin = complimentary watch (friends, referrals, tests)
        db.activate(conn, sub_id, s.watch_days)
        hooks.emit("subscription.activated", conn, sub_id=sub_id, via="admin" if s.payments_on else "free")
        _welcome(sub_id)
        return {"status": "active", "id": sub_id, "sig": signing.sign(sub_id, "watch")}
    return {"status": "pending", "id": sub_id, "checkout_url": create_checkout(sub_id, email, watches, lang=lang)}


def create_checkout(sub_id: str, email: str | None, watches: list, title: str = "GetDuoNow pickup alerts",
                    lang: str = "en") -> str:
    """Stripe Checkout for one watch pass; the webhook activates `sub_id`. Reused by features (e.g. extend)."""
    import stripe
    stripe.api_key = s.stripe_key
    stores_n = len({w[0] for w in watches})
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=email,
        line_items=[{"quantity": 1, "price_data": {
            "currency": "usd", "unit_amount": s.price_cents,
            "product_data": {"name": title,
                             "description": f"{s.watch_days} days · {len(watches)} model/store watches · {stores_n} store(s)"}}}],
        metadata={"sub_id": sub_id},
        locale=i18n.STRIPE_LOCALE.get(lang, "auto"),     # the payment page speaks the language picked on our site
        # The Stripe account (shared with SoleScout) defaults to Managed Payments, which rejects products without a
        # tax_code; we are the merchant of record, so opt out per request (same as SoleScout's checkout).
        managed_payments={"enabled": False},
        success_url=f"{s.base_url}/done?sub={sub_id}&sig={signing.sign(sub_id, 'watch')}",
        cancel_url=f"{s.base_url}/",
    )
    db.set_checkout_session(conn, sub_id, session.id)
    return session.url


@app.post("/api/admin/expire/{sub_id}")
def admin_expire(sub_id: str, request: Request) -> dict:
    if not _is_admin(request):
        raise HTTPException(401, "admin only")
    return {"expired": db.expire(conn, sub_id)}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    import stripe
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), s.stripe_webhook_secret)
    except Exception as e:  # noqa: BLE001 — bad signature / payload
        raise HTTPException(400, "Invalid webhook") from e
    if event["type"] == "checkout.session.completed":
        _activate_paid(event["data"]["object"])
    return {"ok": True}


def _activate_paid(session) -> bool:
    """A paid Checkout Session starts its watch exactly once — from the webhook or from the success page's own check."""
    session = session.to_dict() if hasattr(session, "to_dict") else session   # stripe ≥12: a Session is not a dict
    sub_id = (session.get("metadata") or {}).get("sub_id")
    if session.get("payment_status") != "paid" or not sub_id:
        return False
    paid_email = ((session.get("customer_details") or {}).get("email") or "").strip().lower()
    if paid_email:   # Checkout always asks for an email: it becomes the backup channel of a text-only customer
        conn.execute("UPDATE subscriptions SET email=? WHERE id=? AND COALESCE(email, '')=''", (paid_email, sub_id))
        conn.commit()
    if not db.activate(conn, sub_id, s.watch_days, checkout_session=session.get("id")):
        return False
    hooks.emit("subscription.activated", conn, sub_id=sub_id, via="stripe")
    _welcome(sub_id)
    return True


_confirm_at: dict[str, float] = {}


def confirm_payment(sub_id: str) -> None:
    """While a watch is pending, the success page asks Stripe itself, so a late or lost webhook never leaves a paying
    customer waiting. At most one Stripe call per watch every 3 s."""
    sub = db.get_subscription(conn, sub_id)
    if not sub or sub["status"] != "pending" or not sub["checkout_session"] or not s.stripe_key:
        return
    if time.time() - _confirm_at.get(sub_id, 0) < 3:
        return
    _confirm_at[sub_id] = time.time()
    import stripe
    stripe.api_key = s.stripe_key
    try:
        session = stripe.checkout.Session.retrieve(sub["checkout_session"])
    except Exception as e:  # noqa: BLE001 — Stripe down: the webhook still activates it
        print(f"confirm_payment {sub_id[:8]}: {type(e).__name__}", flush=True)
        return
    meta = (session.to_dict() if hasattr(session, "to_dict") else session).get("metadata") or {}
    if meta.get("sub_id") == sub_id:
        _activate_paid(session)
