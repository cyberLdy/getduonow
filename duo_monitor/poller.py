"""Polling loop: for every store someone watches, check its watched parts and alert subscribers when a cell is in stock.

Each subscription gets at most one alert per in-stock stretch of a cell (tracked by `availability.since`), so a
new subscriber to a cell that is already in stock is alerted on the next poll, and nobody is alerted twice
for the same restock (a failed send is retried on later passes, up to db.MAX_ATTEMPTS per stretch).

A pass: fetch every (store, ≤8 parts) batch concurrently → write availability in one short transaction and commit →
only then send alerts (no write lock is held while waiting on Apple or on SMS/email providers).

    python -m duo_monitor.poller            # run forever
    python -m duo_monitor.poller --once     # one pass
"""
from __future__ import annotations

import argparse
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import db, hooks, messages
from .apple import MAX_PARTS, AppleClient
from .config import Settings
from .notify import Notifier

log = logging.getLogger("duo_monitor.poller")
FETCH_WORKERS = 8      # concurrent Apple requests per pass
JITTER = 3.0           # ± seconds added to the sleep between passes
last_pass_at: float | None = None   # time.time() when run_forever last completed a pass (read by /healthz)
last_stats: dict = {}                # that pass's stats: Apple requests vs errors tell a blocked IP from a quiet night
nearby: dict[str, frozenset[str]] = {}   # anchor store -> stores Apple answered with for it last time (learned)


def chunks(items: list[str], n: int = MAX_PARTS) -> list[list[str]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def plan(targets: dict[str, set[str]]) -> list[tuple[str, list[str]]]:
    """Fewest Apple requests for a pass (Apple refuses an IP that asks too often). One answer for a store covers its
    ~12 nearest stores, so watched stores Apple answered with together last time share one request, their parts
    merged (≤8 per batch). A store not seen yet anchors its own request, and its answer teaches us its neighbours."""
    left, jobs = set(targets), []
    while left:
        anchor = max(sorted(left), key=lambda st: len(nearby.get(st, frozenset()) & left))
        group = (nearby.get(anchor, frozenset()) & left) | {anchor}
        jobs += [(anchor, batch) for batch in chunks(sorted(set().union(*(targets[st] for st in group))))]
        left -= group
    return jobs


def fetch_all(client: AppleClient, jobs: list[tuple[str, list[str]]]) -> list:
    """Fetch every (store, parts) batch concurrently; HTTP only, no DB work off the calling thread.
    → [((store, parts), cells | None, error | None)] in job order."""
    def fetch(job):
        try:
            return job, client.availability(*job), None
        except Exception as e:  # noqa: BLE001 — one bad store/batch must not stop the pass
            return job, None, e
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(jobs)), thread_name_prefix="apple") as ex:
        return list(ex.map(fetch, jobs))


def poll_once(conn, client: AppleClient, notifier: Notifier) -> dict:
    t0 = time.monotonic()
    at = db.now()
    stats = {"expired": db.expire_old(conn, at), "requests": 0, "cells": 0, "in_stock": 0, "alerts": 0, "errors": 0}
    if stats["expired"]:
        hooks.emit("subscriptions.expired", conn, count=stats["expired"])
    targets = db.watch_targets(conn)
    results = fetch_all(client, plan(targets))
    soldout, in_stock = [], []
    with conn:          # one short write transaction (rolled back on error), closed before any hook or message runs
        for (store, batch), cells, err in results:
            if err is not None:
                stats["errors"] += 1
                log.warning("%s %s: %s", store, batch, err)
                continue
            stats["requests"] += 1
            nearby[store] = frozenset(c.store for c in cells) | {store}
            for c in cells:
                if c.part not in targets.get(c.store, ()):    # a neighbour's answer for a part nobody watches there
                    continue
                stats["cells"] += 1
                prev = db.get_availability(conn, c.store, c.part)
                since = db.upsert_availability(conn, store=c.store, part=c.part, available=c.available,
                                               store_name=c.store_name, quote=c.quote, at=at, commit=False)
                if c.available:
                    in_stock.append((c, since))
                elif prev and prev["available"]:
                    soldout.append((c, prev["since"]))
    for c, since in in_stock:
        stats["in_stock"] += 1
        for sub in db.subscribers_to_alert(conn, c.store, c.part, since):
            subject, body = messages.alert(sub["product"], sub["carrier"], c.store_name, c.city, c.quote,
                                           c.part, sub["lang"])
            ok, detail = notifier.alert(sub, body, subject, kind="alert",
                                        sms=messages.alert_sms(sub["product"], c.store_name, c.part, sub["lang"]))
            db.record_alert(conn, sub_id=sub["id"], store=c.store, part=c.part, ok=ok, detail=detail, at=db.now())
            hooks.emit("alert.sent", conn, sub_id=sub["id"], store=c.store, part=c.part, ok=ok, detail=detail)
            stats["alerts"] += ok
    for c, prev_since in soldout:     # after the restock alerts: its handlers send non-urgent "sold out" follow-ups
        minutes = int((at - datetime.fromisoformat(prev_since)).total_seconds() // 60)
        hooks.emit("cell.soldout", conn, store=c.store, part=c.part, store_name=c.store_name,
                   since=prev_since, minutes=minutes)
    stats["round_ms"] = int((time.monotonic() - t0) * 1000)
    hooks.emit("poll.pass", conn, stats=stats)
    return stats


def run_forever(s: Settings, once: bool = False, client: AppleClient | None = None) -> None:
    """`client`: share the web app's, so store lookups and stock checks see the same resting exits."""
    global last_pass_at, last_stats
    from .features import load
    conn = db.connect(s.db_path)
    load(conn)          # feature hooks (alert.sent handlers, footers) must be registered in this process too
    client, notifier = client or AppleClient(list(s.proxies) or None), Notifier(s, conn)
    while True:
        t0 = time.time()
        try:
            last_stats = poll_once(conn, client, notifier)
            log.info("pass %s (%.1fs)", last_stats, time.time() - t0)
            last_pass_at = time.time()
        except Exception:  # noqa: BLE001 — keep the loop alive; one bad pass must not stop alerts
            log.exception("pass failed")
        if once:
            return
        time.sleep(max(1.0, s.poll_seconds - (time.time() - t0) + random.uniform(-JITTER, JITTER)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run_forever(Settings(), once=a.once)


if __name__ == "__main__":
    main()
