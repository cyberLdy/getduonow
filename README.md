# GetDuoNow

[![CI](https://github.com/cyberLdy/getduonow/actions/workflows/ci.yml/badge.svg)](https://github.com/cyberLdy/getduonow/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.14-blue)
![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)

## Get your iPhone Duo ASAP ⚡

**GetDuoNow checks whether iPhone Duo is available for in-store pickup at your Apple Store every 30 seconds, and
texts you the second it's back.** Restocks can vanish in about a minute; this is how you get there first.

Live at [duo.solescout.ai](https://duo.solescout.ai). $4.99 for 14 days, no account, nine languages.
Demo video: [Instagram Reel](https://www.instagram.com/reel/DfY_GirmT2C/).

> **Hosted or self-hosted.** The code is open source under AGPL-3.0. If you'd rather not run a server, outbound IPs
> that Apple won't rate-limit, a registered SMS sender and Stripe, use the hosted version at
> [duo.solescout.ai](https://duo.solescout.ai).

![GetDuoNow](duo_monitor/static/og.png)

## What it does

1. Pick the iPhone Duo models you want: color (Star White / Night Sky) × storage (256 GB / 512 GB / 1 TB), and your
   carrier version.
2. Enter a ZIP code. You get the ~12 nearest Apple Stores; pick the ones you can get to.
3. Pay once through Stripe Checkout (card, Apple Pay, Link).
4. Every 30 seconds the poller asks Apple whether each watched model is available for in-store pickup at each chosen
   store. The moment one is, you get a text (and an email as backup) with a direct link to order it.
5. One alert per restock. When it sells out and comes back, you get alerted again.

You order from Apple yourself. GetDuoNow only tells you when; it never buys on your behalf.

## How pickup availability is checked

Everything comes from one public Apple Store endpoint. It is plain HTTP and needs no browser.

```text
GET https://www.apple.com/shop/retail/pickup-message
      ?pl=true
      &store=R300              one store ...
      &searchNearby=true       ... plus the ~11 stores nearest it, so one request covers a whole metro
      &parts.0=MK1G4LL/A       part numbers, up to 8 per request (Apple silently drops the 9th onward)
      &parts.1=MK1J4LL/A

      or &location=10001 instead of &store=...   ZIP or "City, ST": the ~12 stores nearest that place
```

Reading the response (`duo_monitor/apple.py`, `parse_pickup`):

| field | meaning |
|---|---|
| `body.stores[].storeNumber`, `storeName`, `city`, `storedistance` | the store and its distance from the search point |
| `body.stores[].partsAvailability[<part>].pickupDisplay` | `"available"` means it can be picked up; anything else (`"unavailable"`) means no |
| `...pickupSearchQuote` | human text, e.g. "Available Today" or "Currently unavailable" |
| `...messageTypes.compact.storePickupProductTitle` | product name, e.g. "iPhone Duo 512GB Star White" |

Other facts the code relies on:

- **Model catalog:** each buy page (`/shop/buy-iphone/<slug>`) embeds a `<script id="metrics">` JSON with every part
  number and price. It is fetched once and cached for an hour (`parse_catalog`).
- **Carrier doesn't matter:** pickup stock is per hardware part. The carrier parameter (`cppart`) is ignored, so the
  carrier a customer picks is only shown in their alert.
- **Avoid the newer endpoint:** `/shop/fulfillment-messages` sits behind Apple's bot shield and answers HTTP 541 to
  non-browser clients.
- **No store in range:** for places with no Apple Store nearby (e.g. Montana), Apple omits the store list. That is
  treated as "no stores", not as an error.

## The hard part: rate limits

Apple answers **HTTP 541** to an IP that asks too often and keeps refusing it for about 30 minutes. In production
that showed up as waves roughly every 70 minutes in which up to 95% of checks failed. A green health check once hid
nine hours of this.

What the code does about it:

- **One request per metro, not per store.** The poller groups watched stores by the neighbors Apple returned last
  time and asks once with `searchNearby=true` (`poller.plan`). Only the watched cells are stored.
- **Refused exits rest.** A refused outbound IP is benched for 5 minutes, doubling to at most 15. One success clears
  it. A refused check is retried once on the next exit, so one bad exit doesn't cost a 30-second cycle.
- **Optional proxies.** Set `PROXIES` to a comma-separated list of `http://user:pass@host:port`. They rotate per
  request, with one pooled keep-alive connection per exit. **No proxies are included in this repo; bring your own.**
- **Health that knows when it's blind.** `/healthz` reports requests, errors, exits and how many are resting.
  `scripts/watchdog.py` emails the owner when every check in a pass fails or the poller stalls.

Capacity, measured honestly: with 9 outbound IPs at a 30-second cadence, about one metro area was covered reliably.
More metros need more exits or a slower cadence.

## Architecture

FastAPI + SQLite on a single small instance. The poller runs as a thread inside the web process (`RUN_POLLER=1`)
or as its own process (`python -m duo_monitor.poller`).

| file | role |
|---|---|
| `duo_monitor/apple.py` | Apple client: pickup availability, stores near a ZIP, model catalog, exit rotation and resting |
| `duo_monitor/poller.py` | polling loop; groups stores per metro; decides who is owed an alert |
| `duo_monitor/db.py` | SQLite: subscriptions, watches, last-seen availability, alerts, events |
| `duo_monitor/notify.py` | SMS (Twilio, Textbelt or a JSONL outbox) and email (Resend or SMTP), console fallback |
| `duo_monitor/web.py` | FastAPI app: sign-up flow, store lookup, Stripe Checkout and webhook, health |
| `duo_monitor/messages.py`, `i18n.py`, `locales/` | every page, email and SMS string in 9 languages (en, zh, zh-Hant, es, vi, ko, ja, ar, fa) |
| `duo_monitor/signing.py` | signed links for unsubscribe / extend / "I got it" pages, no login needed |
| `duo_monitor/ratelimit.py` | per-visitor limits plus a global budget on Apple calls |
| `duo_monitor/hooks.py`, `features/` | plugins: admin dashboard, backups, extension, follow-ups, refund guarantee, opt-out, referrals, success stories, restock log |
| `scripts/` | watchdog, nightly backup pull, Twilio toll-free go-live automation, SMS relay, proof uploader |
| `docs/SPEC.md` | the full product spec (in Chinese) |

Things worth reading in the code:

- **Alert exactly once per restock.** "Owed an alert" is a query over the current in-stock stretch, not a flag, so
  repeat polls, new subscribers, failed sends and the next restock all follow from one rule.
- **No lock held across the network.** A pass fetches concurrently, commits in one short transaction, then sends.
- **Payments that can't be lost.** The Stripe webhook and the success page race to activate a watch through one
  guarded update; the success page also asks Stripe directly, so a late webhook can't strand a paying customer.
- **SMS consent done properly.** The confirmation text names the exact models and stores; texts start only after a
  YES, in any of the nine languages. STOP works from any state. Inbound webhooks are signed and replay-protected.

## Notifications

- **SMS:** Twilio with your own verified toll-free number if set, otherwise Textbelt's shared sender pool, otherwise
  a JSONL outbox (`SMS_OUTBOX`) that an external sender drains.
- **Email:** Resend if set, otherwise SMTP, otherwise printed to the console.
- **Double opt-in:** the SMS box on the form is unticked by default. After payment the customer gets "Reply YES to
  start text alerts for <models> at <stores>", and texting only starts after the reply.
- **Refund guarantee:** if nothing restocks during the watch, the watch is extended once for free, then refunded.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                 # empty keys = free mode, alerts printed to the console
set -a; . ./.env; set +a
RUN_POLLER=1 .venv/bin/uvicorn duo_monitor.web:app --port 8000
.venv/bin/python -m pytest -q        # 188 tests, about 3 seconds
```

Open http://localhost:8000, enter a ZIP code and start a watch. From a home connection Apple usually answers
directly. From a cloud server it often answers 541; that is what `PROXIES` is for.

Settings are environment variables; see [`.env.example`](.env.example). Going live needs `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET` (webhook on `checkout.session.completed`), an SMS sender and an email sender.

## Real results

| A real text alert | An order placed 4 minutes after an alert |
|---|---|
| ![Real text alert](duo_monitor/static/proof/03-real-text-alert.webp) | ![Order after alert](duo_monitor/static/proof/04-ordered-4-min-after-alert.webp) |

Store names and order numbers are blurred.

- Built in 6 days (49 commits, Oct 31 to Nov 5, 2026): about 3,000 lines of application code, 2,700 lines of tests.
- In production it caught 12 restocks. The first real alert's restock sold out in about a minute.

## Disclaimer

Not affiliated with, endorsed by or sponsored by Apple Inc. iPhone and Apple Store are trademarks of Apple Inc. This
project only reads publicly available pickup information and asks customers to order from Apple themselves. Keep
request rates low.

## License

[GNU AGPL-3.0](LICENSE). You can use, modify and self-host GetDuoNow. If you run a modified version as a network
service for others, you must publish your changes under the same license.

The hosted service at [duo.solescout.ai](https://duo.solescout.ai) is run by the author. For a commercial license
without the AGPL obligations, contact support@solescout.ai.
