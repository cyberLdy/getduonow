"""Apple Store data: in-store pickup availability, nearby stores, and the model catalog.

Pickup availability comes from the legacy `/shop/retail/pickup-message` endpoint. It is plain HTTP (no
browser, no bot-shield challenge), answers up to 8 parts per request, and can be scoped either to one store
(`store=R300`) or to the ~12 stores nearest a ZIP / city (`location=10001`). The newer
`/shop/fulfillment-messages` endpoint sits behind Apple's shield and returns 541 to non-browser clients.

A cell is in stock when `partsAvailability[part].pickupDisplay == "available"`
(quote "Available Today"); out of stock reads "unavailable" / "Currently unavailable".
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

import httpx

BASE = "https://www.apple.com"
MAX_PARTS = 8          # the endpoint silently drops parts past the 8th
# Apple refuses (541) an IP that asks too often, for ~30 min, and a refused request retried on the next exit pushes
# that one over too (11-04/05: waves every ~70 min, 95% of checks failing). A refused/dead exit rests instead:
# BENCH_S at first, doubling while it keeps being refused, at most BENCH_MAX_S; one success clears it. The cap is
# short on purpose: a wave refuses every exit for ~30 min, and exits still resting after it ends leave all checks
# on the few awake ones (11-05: 8/9 resting after the wave, 1 exit carrying every check).
BENCH_S, BENCH_MAX_S = 300.0, 900.0
REST_STATUS = {403, 429}                      # plus any 5xx (541 is Apple's refusal)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


@dataclass(frozen=True)
class Cell:
    store: str
    store_name: str
    city: str
    part: str
    product: str
    available: bool
    quote: str


@dataclass(frozen=True)
class Store:
    number: str
    name: str
    city: str
    state: str
    miles: float | None


@dataclass(frozen=True)
class Product:
    part: str
    name: str
    price: float | None


def parse_pickup(payload: dict) -> list[Cell]:
    cells = []
    for st in payload["body"]["stores"]:
        for part, a in (st.get("partsAvailability") or {}).items():
            title = (a.get("messageTypes", {}).get("compact", {}) or {}).get("storePickupProductTitle", part)
            cells.append(Cell(store=st["storeNumber"], store_name=st["storeName"], city=st.get("city", ""),
                              part=part, product=title, available=a.get("pickupDisplay") == "available",
                              quote=a.get("pickupSearchQuote", "")))
    return cells


def parse_stores(payload: dict) -> list[Store]:
    """Stores near a ZIP / city. No Apple Store in range (e.g. Montana) = Apple omits the list: no stores, not an error."""
    return [Store(number=st["storeNumber"], name=st["storeName"], city=st.get("city", ""), state=st.get("state", ""),
                  miles=st.get("storedistance")) for st in (payload.get("body") or {}).get("stores") or []]


def parse_catalog(html: str) -> list[Product]:
    """Models listed on a buy page (`/shop/buy-iphone/<slug>`), from its `<script id="metrics">` JSON."""
    m = re.search(r'<script type="application/json" id="metrics">(.*?)</script>', html, re.S)
    if not m:
        return []
    out, seen = [], set()
    for p in json.loads(m.group(1)).get("data", {}).get("products", []):
        if p.get("partNumber") and p["partNumber"] not in seen:
            seen.add(p["partNumber"])
            out.append(Product(part=p["partNumber"], name=p.get("name", p["partNumber"]),
                               price=(p.get("price") or {}).get("fullPrice")))
    return out


class AppleClient:
    """Thin HTTP client, safe to share between threads. `proxies` (optional) are rotated round-robin, one per request,
    skipping exits that are resting after a refusal (see BENCH_S).

    Keeps one pooled (keep-alive) httpx.Client per proxy — just one without proxies — created on first use, instead of
    a new connection + TLS handshake for every request."""

    def __init__(self, proxies: list[str] | None = None, timeout: float = 20):
        self._proxies = list(proxies or [])
        self._next = 0
        self._rest: dict[str, tuple[float, float]] = {}   # proxy -> (resting until, on time.monotonic(); rest length)
        self.timeout = timeout
        self._clients: dict[str | None, httpx.Client] = {}
        self._lock = threading.Lock()

    def _pick(self) -> str | None:
        """Next exit in rotation that is not resting; with every exit resting, the one back soonest."""
        with self._lock:
            if not self._proxies:
                return None
            now, n = time.monotonic(), len(self._proxies)
            for k in range(n):
                p = self._proxies[(self._next + k) % n]
                if self._rest.get(p, (0.0, 0.0))[0] <= now:
                    self._next = (self._next + k + 1) % n
                    return p
            return min(self._proxies, key=lambda p: self._rest[p][0])

    def _refused(self, proxy: str | None) -> None:
        if proxy is None:
            return
        with self._lock:
            last = self._rest.get(proxy, (0.0, 0.0))[1]
            secs = min(BENCH_MAX_S, last * 2) if last else BENCH_S
            self._rest[proxy] = (time.monotonic() + secs, secs)

    def _answered(self, proxy: str | None) -> None:
        with self._lock:
            self._rest.pop(proxy, None)

    def resting(self) -> int:
        """How many exits are resting right now (for /healthz)."""
        now = time.monotonic()
        with self._lock:
            return sum(1 for until, _ in self._rest.values() if until > now)

    def _client_for(self, proxy: str | None) -> httpx.Client:
        with self._lock:
            if proxy not in self._clients:
                self._clients[proxy] = httpx.Client(proxy=proxy, timeout=self.timeout, headers={"User-Agent": UA})
            return self._clients[proxy]

    def _client(self) -> httpx.Client:
        return self._client_for(self._pick())

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """With proxies, a refused or dead exit rests and the request is retried once through the next awake one."""
        tries = 2 if self._proxies else 1
        for attempt in range(tries):
            proxy = self._pick()
            try:
                r = self._client_for(proxy).get(BASE + path, params=params)
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in REST_STATUS or code >= 500:
                    self._refused(proxy)
                if attempt == tries - 1:
                    raise
                continue
            except httpx.TransportError:
                self._refused(proxy)
                if attempt == tries - 1:
                    raise
                continue
            self._answered(proxy)
            return r
        raise AssertionError("unreachable")

    def _pickup(self, parts: list[str], **where) -> dict:
        if not 0 < len(parts) <= MAX_PARTS:
            raise ValueError(f"1..{MAX_PARTS} parts per request, got {len(parts)}")
        params = {"pl": "true", **where, **{f"parts.{i}": p for i, p in enumerate(parts)}}
        return self._get("/shop/retail/pickup-message", params).json()

    def availability(self, store: str, parts: list[str]) -> list[Cell]:
        """`parts` at `store` and at the ~11 stores nearest it (searchNearby): one request covers a metro area."""
        return parse_pickup(self._pickup(parts, store=store, searchNearby="true"))

    def availability_near(self, location: str, parts: list[str]) -> tuple[list[Store], list[Cell]]:
        """Stores nearest a ZIP/city plus live pickup availability of `parts` there (≤8 parts per request)."""
        stores: list[Store] = []
        cells: list[Cell] = []
        for i in range(0, len(parts), MAX_PARTS):
            payload = self._pickup(parts[i:i + MAX_PARTS], location=location)
            near = parse_stores(payload)
            if not near:
                return [], []
            stores = stores or near
            cells += parse_pickup(payload)
        return stores, cells

    def stores_near(self, location: str, part: str) -> list[Store]:
        return parse_stores(self._pickup([part], location=location))

    def catalog(self, slug: str) -> list[Product]:
        return parse_catalog(self._get(f"/shop/buy-iphone/{slug}").text)
