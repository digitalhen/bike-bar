"""Starlette app: clean REST facade over Bosch eBike Flow + static dashboard."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from . import auth, config, poller, tracks, webhooks
from .bosch import BoschClient, BoschError
from .store import TokenStore

_ROOT = Path(__file__).resolve().parent.parent
STORE = TokenStore()


# --- helpers ----------------------------------------------------------------
def _client(request) -> BoschClient:
    user = request.headers.get("X-Bike-User") or request.query_params.get("user") \
        or STORE.default_user()
    if not user:
        raise BoschError(401, "no user configured — log in via /api/auth/login")
    fresh = request.query_params.get("fresh") in ("1", "true", "yes")
    return BoschClient(STORE, user, request.app.state.http, fresh=fresh)


def _err(e: BoschError) -> JSONResponse:
    return JSONResponse({"error": e.detail, "status": e.status}, status_code=e.status)


async def _guard(coro):
    try:
        return None, await coro
    except BoschError as e:
        return _err(e), None


# --- auth routes ------------------------------------------------------------
async def auth_login(request):
    verifier, challenge = auth.pkce_pair()
    import secrets
    state = secrets.token_urlsafe(16)
    STORE.stash_verifier(state, verifier)
    url = auth.build_auth_url(challenge, state)
    return JSONResponse({
        "auth_url": url,
        "state": state,
        "instructions": "Open in a desktop browser with DevTools Network tab. "
                        "Log in, then copy the 'code' param from the failed "
                        "onebikeapp-ios:// redirect and POST it to /api/auth/callback.",
    })


# tracks the most recent auto-capture result so the login UI can poll for it
_LAST_LOGIN: dict = {}


def _parse_code_state(raw: str, state: str | None) -> tuple[str, str | None]:
    """Accept a bare code, or a full onebikeapp-ios://...?code=..&state=.. URL."""
    raw = (raw or "").strip()
    if "code=" in raw:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(raw).query)
        return q.get("code", [raw])[0], (q.get("state", [state]) or [state])[0]
    return raw, state


async def _complete_login(http, code: str, state: str | None) -> dict:
    verifier = STORE.pop_verifier(state) if state else None
    if not verifier:
        raise BoschError(400, "unknown or expired login state")
    tokens = await auth.exchange_code(http, code, verifier)
    user_id = STORE.put(tokens)
    client = BoschClient(STORE, user_id, http)
    bikes = await client.bikes()
    return {"user": user_id, "bikes": bikes}


async def auth_callback(request):
    """Manual paste fallback: {code|url, state}."""
    body = await request.json()
    code, state = _parse_code_state(body.get("code", ""), body.get("state"))
    try:
        result = await _complete_login(request.app.state.http, code, state)
    except BoschError as e:
        return _err(e)
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": e.response.text[:200]}, status_code=400)
    return JSONResponse(result)


async def auth_redirect(request):
    """Auto-capture target: the macOS scheme handler POSTs the deep-link URL here.
    Also accepts a browser GET with ?code=&state=."""
    if request.method == "POST":
        body = await request.json()
        raw, state = body.get("url") or body.get("code", ""), body.get("state")
    else:
        raw, state = request.query_params.get("code", ""), request.query_params.get("state")
    code, state = _parse_code_state(raw, state)
    try:
        result = await _complete_login(request.app.state.http, code, state)
        _LAST_LOGIN.clear(); _LAST_LOGIN.update({"ok": True, **result})
    except (BoschError, httpx.HTTPStatusError) as e:
        detail = e.detail if isinstance(e, BoschError) else e.response.text[:200]
        _LAST_LOGIN.clear(); _LAST_LOGIN.update({"ok": False, "error": detail})
        return JSONResponse({"error": detail}, status_code=400)
    return JSONResponse(result)


async def auth_status(request):
    """Login UI polls this to learn when an auto-capture completed."""
    return JSONResponse({"logged_in": bool(STORE.default_user()),
                         "user": STORE.default_user(), "last": _LAST_LOGIN})


# --- data routes ------------------------------------------------------------
async def list_bikes(request):
    err, data = await _guard(_client(request).bikes())
    return err or JSONResponse(data)


async def get_battery(request):
    err, data = await _guard(_client(request).battery(request.path_params["bike_id"]))
    return err or JSONResponse(data)


async def get_profile(request):
    err, data = await _guard(_client(request).profile(request.path_params["bike_id"]))
    return err or JSONResponse(data)


async def get_debug_battery(request):
    err, data = await _guard(_client(request).debug_battery(request.path_params["bike_id"]))
    return err or JSONResponse(data)


async def get_stats(request):
    err, data = await _guard(_client(request).stats(request.path_params["bike_id"]))
    return err or JSONResponse(data)


async def get_rides(request):
    gps = request.query_params.get("gps") in ("1", "true", "yes")
    err, data = await _guard(_client(request).rides(request.path_params["bike_id"], with_gps=gps))
    return err or JSONResponse(data)


async def get_ride(request):
    p = request.path_params
    err, data = await _guard(_client(request).ride(p["bike_id"], p["aid"]))
    return err or JSONResponse(data)


async def get_ride_gpx(request):
    p = request.path_params
    err, data = await _guard(_client(request).ride(p["bike_id"], p["aid"]))
    if err:
        return err
    return Response(tracks.to_gpx(data), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{p["aid"]}.gpx"'})


async def get_tracks_geojson(request):
    client = _client(request)
    bike_id = request.path_params["bike_id"]
    err, rides = await _guard(client.rides(bike_id))
    if err:
        return err
    full = []
    for r in rides:
        _, detail = await _guard(client.ride(bike_id, r["id"]))
        if detail and detail.get("has_gps"):
            full.append(detail)
    return JSONResponse(tracks.to_geojson(full))


async def get_location(request):
    err, data = await _guard(_client(request).last_location(request.path_params["bike_id"]))
    if err:
        return err
    return JSONResponse(data or {"error": "no GPS-recorded rides yet"},
                        status_code=200 if data else 404)


# --- webhooks + events ------------------------------------------------------
async def webhooks_list(request):
    return JSONResponse(webhooks.list_subs())


async def webhooks_create(request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "a valid http(s) url is required"}, status_code=400)
    sub = webhooks.add_sub(url, body.get("events"), body.get("secret"))
    # return the secret once so the caller can verify signatures
    return JSONResponse({"id": sub["id"], "url": sub["url"], "events": sub["events"],
                         "secret": sub["secret"]}, status_code=201)


async def webhooks_delete(request):
    ok = webhooks.delete_sub(request.path_params["sub_id"])
    return JSONResponse({"deleted": ok}, status_code=200 if ok else 404)


async def webhooks_test(request):
    sub = webhooks.get_sub(request.path_params["sub_id"])
    if not sub:
        return JSONResponse({"error": "unknown webhook"}, status_code=404)
    payload = {"event": "test.ping", "at": __import__("time").time(),
               "data": {"message": "hello from bosch-flow"}}
    result = await webhooks.deliver(request.app.state.http, sub, payload)
    return JSONResponse(result, status_code=200 if result["ok"] else 502)


async def events_list(request):
    limit = int(request.query_params.get("limit", 50))
    return JSONResponse(poller.recent_events(limit))


async def poll_now(request):
    """Manually trigger one poll cycle (handy for testing)."""
    dispatch = request.query_params.get("dispatch", "1") not in ("0", "false", "no")
    fired = await poller.poll_once(STORE, request.app.state.http, dispatch=dispatch)
    return JSONResponse({"detected": fired})


# --- shared UI preferences (units) ------------------------------------------
# One small JSON file both the dashboard and the menu bar app read/write, so a
# km/mi switch in either place is reflected in the other.
import json as _json

_VALID_UNITS = {"metric", "imperial"}


def _read_prefs() -> dict:
    p = config.data_path(config.PREFS_FILE)
    if p.exists():
        try:
            data = _json.loads(p.read_text())
        except (ValueError, OSError):
            data = {}
    else:
        data = {}
    units = data.get("units")
    return {"units": units if units in _VALID_UNITS else "metric"}


async def prefs_get(request):
    return JSONResponse(_read_prefs())


async def prefs_put(request):
    body = await request.json()
    units = body.get("units")
    if units not in _VALID_UNITS:
        return JSONResponse({"error": "units must be 'metric' or 'imperial'"}, status_code=400)
    prefs = {"units": units}
    config.data_path(config.PREFS_FILE).write_text(_json.dumps(prefs, indent=2))
    return JSONResponse(prefs)


async def index(request):
    return FileResponse(_ROOT / "web" / "index.html")


# --- app --------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app):
    app.state.http = httpx.AsyncClient(timeout=20)
    app.state.store = STORE
    task = asyncio.create_task(poller.run_loop(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await app.state.http.aclose()


routes = [
    Route("/", index),
    Route("/api/auth/login", auth_login),
    Route("/api/auth/callback", auth_callback, methods=["POST"]),
    Route("/api/auth/redirect", auth_redirect, methods=["GET", "POST"]),
    Route("/api/auth/status", auth_status),
    Route("/api/bikes", list_bikes),
    Route("/api/bikes/{bike_id}/battery", get_battery),
    Route("/api/bikes/{bike_id}/profile", get_profile),
    Route("/api/bikes/{bike_id}/debug/battery", get_debug_battery),
    Route("/api/bikes/{bike_id}/stats", get_stats),
    Route("/api/bikes/{bike_id}/rides", get_rides),
    Route("/api/bikes/{bike_id}/rides/{aid}.gpx", get_ride_gpx),
    Route("/api/bikes/{bike_id}/rides/{aid}", get_ride),
    Route("/api/bikes/{bike_id}/tracks.geojson", get_tracks_geojson),
    Route("/api/bikes/{bike_id}/location", get_location),
    Route("/api/webhooks", webhooks_list),
    Route("/api/webhooks", webhooks_create, methods=["POST"]),
    Route("/api/webhooks/{sub_id}", webhooks_delete, methods=["DELETE"]),
    Route("/api/webhooks/{sub_id}/test", webhooks_test, methods=["POST"]),
    Route("/api/events", events_list),
    Route("/api/poll", poll_now, methods=["POST"]),
    Route("/api/prefs", prefs_get),
    Route("/api/prefs", prefs_put, methods=["PUT"]),
]

if (_ROOT / "web").exists():
    routes.append(Mount("/static", app=StaticFiles(directory=_ROOT / "web"), name="static"))

app = Starlette(routes=routes, lifespan=lifespan)
