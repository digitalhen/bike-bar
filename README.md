# bike-bar

**Bike Bar** puts your **Bosch eBike Flow** data (Tern GSD and any Bosch Smart
System bike with a ConnectModule) in your Mac's menu bar:

- a native **macOS menu bar app** — live battery %, charge countdown, ride alerts,
  one-click sign-in, and auto-updates,
- a **web dashboard** — battery, per-mode efficiency, bike health, firmware, and a
  metric-colored ride map with GPX export,
- plus, for tinkerers: a **local REST API**, a zero-dependency **CLI**, and
  **event webhooks** (battery full/low, charging, ride completed, firmware…).

<p align="center">
  <img src="docs/dashboard.png" width="820"
    alt="Bike Bar web dashboard: battery, per-mode efficiency, bike health, firmware inventory, and a metric-colored ride map with history">
</p>

> ℹ️ **Same data as the Bosch app.** "Bike Bar" shows the exact battery, range and
> ride info you see in Bosch's **eBike Flow** app — just in your Mac's menu bar. (Named
> "Bosch Flow", then "Bosch Bar", now **Bike Bar** — far enough from Bosch's own Flow
> app to avoid confusion. The bundle on disk is now `Bike Bar.app`, but the bundle id
> is still `com.boschflow.menubar`, so auto-updates keep working across the renames.)

> ⚠️ **Unofficial.** This talks to Bosch's private eBike Flow API using the mobile
> app's OAuth client (reverse-engineered). It is not endorsed by Bosch, may break
> if Bosch changes things, and is intended for personal use with **your own**
> account. See [Limitations](#limitations--caveats).

---

## Install (macOS)

**That's all most people need — download, open, sign in.**

1. **[Download the latest release »](https://github.com/digitalhen/bike-bar/releases/latest)**
   and unzip `Bike-Bar-*.zip`.
2. Drag **`Bike Bar.app`** into your **Applications** folder and open it — it shows
   up everywhere as **Bike Bar**.
3. It appears in your **menu bar** (🚲 + battery %). Click it → **Sign in…**, log in
   with your **eBike Flow** account in the browser that opens — done.

No Python, no terminal, no copying tokens. The app is **Developer ID-signed and
notarized** (opens without warnings) and **auto-updates itself**. On first launch,
click **Allow** when macOS asks about notifications so you get battery and ride alerts.

> Everything below is for **developers / power users** — the CLI, the REST API,
> webhooks, and running the server yourself. If you just want the app, you're done. 🚲

---

## Contents (developers)

1. [Requirements](#requirements)
2. [Setup](#1-setup)
3. [Get a token (authenticate with Bosch)](#2-get-a-token-authenticate-with-bosch)
4. [Run the API + dashboard](#3-run-the-api--dashboard)
5. [Menu bar app (macOS)](#4-menu-bar-app-macos)
6. [Events & webhooks](#5-events--webhooks)
7. [Configuration](#configuration)
8. [API reference](#api-reference)
9. [How it works](#how-it-works)
10. [Limitations & caveats](#limitations--caveats)
11. [Project layout](#project-layout)

---

## Requirements

- **Python 3.9+** (developed on 3.14)
- A **Bosch eBike** with a **ConnectModule** and an **eBike Flow** account
- **macOS** — only needed for the menu bar app + native notifications; the CLI,
  API, and dashboard run anywhere
- For the menu bar app: **Xcode command-line tools** (`swiftc`)

---

## 1. Setup

```bash
git clone <this repo> bosch-flow && cd bosch-flow
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

That's it for the server. The CLI (`bosch_ebike_poc.py`) needs **no dependencies**
at all (pure standard library).

---

## 2. Get a token (authenticate with Bosch)

This is the only fiddly part. Bosch's OAuth client redirects to an **iOS app deep
link** (`onebikeapp-ios://…`) that a browser can't follow, so you have to grab the
authorization `code` out of the browser's developer tools by hand. You do this
**once**; after that the token auto-refreshes.

### Option A — CLI (simplest)

```bash
python3 bosch_ebike_poc.py login
```

It prints a long `https://p9.authz.bosch.com/…` URL. Then:

1. **Open that URL in a desktop browser** (Chrome or Firefox).
2. Open **DevTools → Network tab** *before* logging in, and enable
   **"Preserve log"** (Chrome) / **"Persist Logs"** (Firefox).
3. **Log in** with your **eBike Flow app credentials** (same as the phone app).
4. After login the page tries to redirect to
   `onebikeapp-ios://com.bosch.ebike.onebikeapp/oauth2redirect?code=…` and
   **fails** — that's expected (browsers can't open an iOS app link).
5. In the **Network tab**, find that **failed `onebikeapp-ios://…` request**.
   Copy the value of its **`code`** query parameter — a very long (500–1000+ char)
   string. (You can also copy the whole `onebikeapp-ios://…` URL; the tool will
   extract the code.)
6. **Paste it** back into the CLI prompt and press Enter.

The tokens are saved to **`bosch_tokens.json`**. Verify:

```bash
python3 bosch_ebike_poc.py bikes      # list your bike(s)
python3 bosch_ebike_poc.py fetch      # battery, range, odometer…
```

The API server **auto-imports `bosch_tokens.json`** on first run, so once the CLI
login works, the server and dashboard are authenticated too.

### Option B — via the API server

If you'd rather not use the CLI:

```bash
# 1) start the server (see section 3), then:
curl -s http://127.0.0.1:8099/api/auth/login
#    -> {"auth_url": "...", "state": "..."}
```

Do steps 1–5 above with the returned `auth_url`, then exchange the code:

```bash
curl -s -XPOST http://127.0.0.1:8099/api/auth/callback \
  -H 'content-type: application/json' \
  -d '{"code":"<PASTED_CODE_OR_URL>", "state":"<STATE_FROM_LOGIN>"}'
#    -> {"user": "<rider-id>", "bikes": [...]}
```

Tokens are stored in **`bosch_users.json`** (keyed by your Bosch rider id) and
**auto-refreshed** (the access token lasts ~2h; a refresh token is used silently).

### Where tokens live / resetting

| File | Written by | Contains |
|------|------------|----------|
| `bosch_tokens.json` | CLI `login` | single-user access + refresh token |
| `bosch_users.json`  | API `/auth/callback` | multi-user token store |

Delete these to log out. If a refresh ever fails (e.g. Bosch invalidated it),
just run `login` again.

---

## 3. Run the API + dashboard

> On macOS you can skip this — the [menu bar app](#4-menu-bar-app-macos) embeds and
> runs this backend for you. Use the steps below for development, non-Mac hosts, or
> to run the server headless.

```bash
./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8099 --reload
# or, honoring BOSCH_FLOW_DATA_DIR / HOST / PORT:
./.venv/bin/python serve.py
```

Open **http://127.0.0.1:8099/** — the dashboard shows battery, range per assist
mode, lifetime mode efficiency, firmware inventory, a map of your rides, and GPX
downloads. Hit **⟳ Refresh** to force a fresh pull past the cache.

Click any ride to draw its **GPS track colored by metric** — toggle between
**Speed / Power / Cadence** (top-right of the map) to see where you were fast vs.
where you actually put power down. Deep-link straight to a ride with
`?ride=<activity-id>&metric=power_w`.

> Note: Bosch records the *assist mode* only as a whole-ride total (distance per
> mode), not per GPS point — so the track can be colored by speed/power/cadence,
> but not by which assist mode you were in at each point.

The server also starts a **background poller** (every 5 min) that powers events,
webhooks, and notifications.

---

## 4. Menu bar app (macOS)

A native menu bar app that shows live battery % and posts **native desktop
notifications** on events (this is the reliable way to get banners — a proper app
gets its own notification permission).

<p align="center">
  <img src="docs/menubar.png" width="300"
    alt="Bike Bar menu bar popover: battery, per-mode ranges, odometer, latest ride, sign-in status, launch-at-login and Check for Updates">
</p>

**It's self-contained — no separate server, no Python needed.** The app *embeds*
the whole backend (section 3) as a frozen binary, launches it on startup, and
shuts it down on quit. You can still `curl http://127.0.0.1:8099` or open the
dashboard while the app is running; it's the same REST API, just supervised for
you. Its token/state files live in `~/Library/Application Support/Bike Bar/`
(with a `backend.log` there if you need to debug).

**Prebuilt:** grab the latest signed build from
**[GitHub Releases](https://github.com/digitalhen/bike-bar/releases/latest)** —
unzip and drag to `/Applications` (no Xcode needed). It's **Developer ID**-signed and
**notarized** by Apple, so it opens without warnings, and **auto-updates itself** via
Sparkle from then on. If you build/sign it yourself and macOS flags it, right-click →
**Open** once.

Maintainers: **`menubar/notarize.sh`** does the full Developer-ID sign → notarize →
staple → package flow (needs a `notarytool` keychain profile and an in-effect Apple
Developer agreement). It signs the embedded backend inside-out with the entitlements
CPython needs under the hardened runtime.

**Or build it yourself:**

```bash
cd menubar
../.venv/bin/pip install -r ../requirements-build.txt   # once: PyInstaller
./build.sh                 # compiles Swift + freezes the backend into the .app
open "Bike Bar.app"
```

On first launch macOS asks **"Bike Bar" would like to send notifications** —
click **Allow** (this is what makes banners work).

- Menu bar shows `🚲 100%` (⚡ when charging).
- Click it for a popover: battery, ranges, odometer, latest ride, **Dashboard**
  and **Refresh** buttons.
- **Log in / Update token…** — signs you into Bosch right from the app: it opens
  the Bosch login in your browser and captures the `onebikeapp-ios://` redirect
  automatically (the app registers that URL scheme). If the hand-off doesn't fire,
  **Paste code…** takes the code (or the whole redirect URL) instead. No CLI,
  DevTools, or `curl` needed for setup.
- **Launch at login** — a checkbox that enrolls the app as a macOS login item
  (via `SMAppService`); untick to remove it.
- **Check for Updates…** — the app auto-updates itself via **Sparkle**: it checks
  for a new version in the background and can install it with one click. No App
  Store needed.
- **Share anonymous usage stats** — on by default; untick to opt out (see Privacy).
- New events (battery full, ride logged, …) fire native banners.

### Privacy — anonymous usage stats

So we can see how many installs are actually active, the app sends an anonymous
**"app is running"** ping on launch (and a heartbeat twice a day) to
[Aptabase](https://aptabase.com), a privacy-first analytics service that stores
**no IP address and no device identifier**. The ping carries only the **app
version**, **macOS version**, **locale**, and a **rotating session id** — never
your account, bike, location, rides, or tokens. Turn it off any time with
**Settings → Share anonymous usage stats**; nothing is sent while it's off.

> Building the app requires **PyInstaller** (`requirements-build.txt`) in the venv
> and `swiftc` (Xcode command-line tools). The *runtime* still needs nothing but
> the frozen bundle.

**Auto-update (maintainers).** Updates ship as notarized zips on **GitHub Releases**;
the app's `SUFeedURL` reads `releases/latest/download/appcast.xml`. Cut a release with:

```bash
cd menubar
./make-release.sh <version> <build#> "release notes"
# e.g. ./make-release.sh 1.1 2 "Embedded backend, in-app login, unit sync."
```

That builds + notarizes at the given version (`notarize.sh`), EdDSA-signs the zip for
Sparkle (`sparkle_sign.swift`, using `~/.appstoreconnect/private_keys/sparkle_ed25519.key`),
generates `appcast.xml`, and publishes both to GitHub Releases via `gh`. The **build
number must increase every release** — Sparkle compares it to detect updates. The
Sparkle framework is vendored at `menubar/vendor/Sparkle.framework`; `notarize.sh`
signs its nested helpers inside-out.

---

## 5. Events & webhooks

Bosch's API is **poll-only** (no push). The poller diffs each fresh snapshot and
emits edge-triggered events:

`battery.full` · `battery.low` · `battery.charging_started` ·
`battery.charging_stopped` · `charger.connected` · `charger.disconnected` ·
`ride.completed` · `firmware.changed`

Manage webhooks from the dashboard's **Webhooks** button (add/delete/test, pick
events, see a live event feed), or via the API — register one (fired with an HMAC
signature):

```bash
curl -s -XPOST http://127.0.0.1:8099/api/webhooks \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/hook","events":["battery.full","ride.completed"]}'
#   -> returns a "secret" ONCE; use it to verify signatures
```

Each delivery carries:

```
X-Bosch-Flow-Event: battery.full
X-Bosch-Flow-Signature: sha256=<hmac_sha256(secret, raw_body)>
```

Verify by recomputing the HMAC over the raw request body. See
[API reference](#api-reference) for `GET /api/events`, `POST /api/poll`, etc.

---

## Configuration

Edit `app/config.py`:

| Setting | Default | Meaning |
|---------|---------|---------|
| `POLL_INTERVAL` | `300` | seconds between poll cycles |
| `BATTERY_LOW_PCT` | `20` | threshold for `battery.low` |
| `TTL_LIVE` / `TTL_PROFILE` / `TTL_RIDES` | 300 / 900 / 600 | cache TTLs (s) |
| `NOTIFY_ENABLED` | `False` | fire `terminal-notifier` from the server (the menu bar app handles notifications instead) |
| `WEBHOOK_RETRIES` | `3` | delivery attempts w/ backoff |

The macOS notification sender identity is in `NOTIFY_SENDER` (only used when
`NOTIFY_ENABLED = True`).

**Environment variables** (read by `serve.py`, and set automatically by the menu
bar app):

| Var | Default | Meaning |
|-----|---------|---------|
| `BOSCH_FLOW_DATA_DIR` | repo root | where tokens/state/event log are written (the app points this at `~/Library/Application Support/Bosch Flow`) |
| `BOSCH_FLOW_HOST` | `127.0.0.1` | bind host |
| `BOSCH_FLOW_PORT` | `8099` | bind port |

---

## API reference

| Endpoint | Returns |
|----------|---------|
| `GET /api/bikes` | bikes on the account |
| `GET /api/bikes/{id}/battery` | live SoC, charging, range per mode, odometer |
| `GET /api/bikes/{id}/profile` | model, ABS/alarm, firmware inventory, health |
| `GET /api/bikes/{id}/stats` | per-mode lifetime distance + Wh/km |
| `GET /api/bikes/{id}/rides` | ride list (`?gps=1` adds `has_gps`/`gps_points`) |
| `GET /api/bikes/{id}/rides/{aid}` | ride summary + GPS time series |
| `GET /api/bikes/{id}/rides/{aid}.gpx` | ride as GPX |
| `GET /api/bikes/{id}/tracks.geojson` | all GPS tracks as one layer |
| `GET /api/bikes/{id}/location` | last-known position (newest GPS ride) |
| `GET /api/events?limit=50` | recent event log |
| `POST /api/poll` | run one poll cycle now (`?dispatch=0` = detect only) |
| `GET/POST /api/webhooks`, `DELETE /api/webhooks/{id}`, `POST /api/webhooks/{id}/test` | manage webhooks |
| `GET /api/auth/login`, `POST /api/auth/callback` | authentication |

Add `?fresh=1` to any data endpoint to bypass the cache. Select a user (multi-user
setups) with `X-Bike-User: <rider-id>` or `?user=`.

---

## How it works

Bosch splits data across several hosts under `*.prod.connected-biking.cloud`,
behind an OAuth2 + PKCE Keycloak (`p9.authz.bosch.com`, realm `obc`, client
`one-bike-app`). This project is a **facade** over that:

- **`app/auth.py`** — OAuth2 + PKCE (build URL, exchange code, refresh).
- **`app/bosch.py`** — async client that hides tokens, caches per Bosch's cadence,
  and **normalizes** the raw JSON. Data comes from `obc-rider-profile`
  (`/v1/bike-profile`, `/v1/state-of-charge`) and `obc-rider-activity`
  (`/v1/activity`, `/v1/activity/{id}/detail` — the source of GPS tracks).
- **`app/poller.py`** — background loop → snapshot diff → events → webhooks.

Live data (`state-of-charge`) is only fresh when the ConnectModule is **charging,
powered on, or alarmed**; otherwise you get last-known values.

---

## Limitations & caveats

- **Unofficial / ToS.** Uses the eBike Flow app's OAuth client. Fine for personal
  use with your own account; not a basis for a public multi-user product — that
  needs official [Bosch Connected Biking Platform](https://www.bosch-ebike.com/en/business/connected-biking-platform)
  partner API access (your own client + redirect URIs → a real one-click web login).
- **Login uses the app's deep-link redirect.** There's no hosted browser-redirect
  login, so the CLI/API path relies on copying the `code` from the redirect. The
  **menu bar app closes this loop**: it registers the `onebikeapp-ios://` scheme and
  captures the redirect automatically (with a paste fallback).
- **Location.** GPS tracks exist only for rides recorded with the phone. The app's
  live "where's my bike" pin (anti-theft) is served by a separate host whose exact
  route isn't mapped here.
- **Notifications are local** to whichever machine runs the menu bar app; use
  webhooks to reach elsewhere.
- **Sync lag.** Rides appear only after the bike syncs to Bosch's cloud (open the
  Flow app near the powered-on bike); the odometer updates independently and
  sooner.

---

## Project layout

```
bosch_ebike_poc.py      zero-dependency CLI (login / bikes / fetch / raw)
serve.py                backend entry point (honors BOSCH_FLOW_DATA_DIR/HOST/PORT)
requirements.txt        server deps (starlette, uvicorn, httpx)
requirements-build.txt  build-only deps (PyInstaller, to freeze the backend)
app/
  config.py             hosts, OAuth, cache TTLs, poller + notify, data dir
  auth.py               OAuth2 + PKCE helpers
  store.py              multi-user, file-backed token store
  bosch.py              async facade: raw endpoints + normalization + cache
  tracks.py             GPX / GeoJSON rendering
  events.py             pure event-detection rules (snapshot diff)
  webhooks.py           webhook subscriptions + signed delivery
  poller.py             background poll loop + event log
  notify.py             (optional) server-side macOS notifications
  main.py               Starlette routes + serves the dashboard
web/index.html          the dashboard (Leaflet map + cards)
menubar/
  BoschBar.swift        menu bar app: supervises backend + login + notifications
  makeicon.swift        app-icon generator
  build.sh              compile Swift + freeze backend + bundle + sign the .app
  notarize.sh           Developer-ID sign (inside-out) + notarize + staple
```

Token / state files (git-ignored, created at runtime): `bosch_tokens.json`,
`bosch_users.json`, `bosch_webhooks.json`, `bosch_poller_state.json`,
`bosch_events.jsonl`.
