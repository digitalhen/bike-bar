"""Background poller: fetch fresh state, detect events, dispatch webhooks."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path

import httpx

from . import config, events, notify, prefs, schedules, webhooks
from .bosch import BoschClient, BoschError
from .store import TokenStore

def _state_path() -> Path:
    return config.data_path(config.POLLER_STATE_FILE)


def _log_path() -> Path:
    return config.data_path(config.EVENTS_LOG_FILE)


# in-memory ring of recent events for GET /api/events
_RECENT: deque[dict] = deque(maxlen=config.EVENTS_LOG_MAX)


def _load_state() -> dict:
    p = _state_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(state: dict) -> None:
    _state_path().write_text(json.dumps(state, indent=2))


def recent_events(limit: int = 50) -> list[dict]:
    return list(_RECENT)[-limit:][::-1]


def _log_event(rec: dict) -> None:
    _RECENT.append(rec)
    with _log_path().open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _prime_recent() -> None:
    """Load the tail of the on-disk log into memory on startup."""
    log = _log_path()
    if not log.exists():
        return
    for line in log.read_text().splitlines()[-config.EVENTS_LOG_MAX:]:
        try:
            _RECENT.append(json.loads(line))
        except json.JSONDecodeError:
            pass


async def _snapshot(client: BoschClient, bike_id: str) -> dict:
    battery = await client.battery(bike_id)
    profile = await client.profile(bike_id)
    rides = await client.rides(bike_id)  # cheap: no gps enrichment
    return events.build_snapshot(battery, profile, rides)


async def poll_user(store: TokenStore, http: httpx.AsyncClient, user_id: str,
                    dispatch: bool = True) -> list[dict]:
    """One poll cycle for a user. Returns the events detected."""
    client = BoschClient(store, user_id, http, fresh=True)
    state = _load_state()
    fired: list[dict] = []
    deliveries: list[tuple[dict, dict]] = []  # (subscription, event record)
    for bike in await client.bikes():
        bike_id = bike["id"]
        bike_name = f"{bike.get('brand') or 'eBike'} {bike.get('drive_unit') or ''}".strip()
        key = f"{user_id}:{bike_id}"
        cur = await _snapshot(client, bike_id)
        prev = state.get(key)

        # Target is level-triggered and latched, so it survives a restart taken
        # mid-charge - the one check worth running even on a first sighting,
        # because the cost of missing it is charging the pack to 100%.
        target = prefs.charge_target()
        at_target = events.at_target(cur, target)
        cur["target_fired"] = at_target
        cur["target_fired_at"] = target if at_target else None
        state[key] = cur
        # The latch is only valid for the ceiling it was armed at. Changing the
        # ceiling therefore re-arms it: without this, raising 80 -> 100 left a
        # stale latch that suppressed the next cut entirely, and lowering it
        # again would not take effect until the bike was unplugged.
        latched = (bool((prev or {}).get("target_fired"))
                   and (prev or {}).get("target_fired_at") == target)
        detected = []
        if at_target and not latched:
            detected.append({"event": "battery.target",
                             "data": {"level": cur.get("level"), "target": target,
                                      "charging": bool(cur.get("charging")),
                                      "baseline": prev is None}})
        if prev is not None:
            detected += events.detect(prev, cur)
        elif not detected:
            continue  # first sighting: set a baseline, don't fire

        for ev in detected:
            rec = {
                "event": ev["event"],
                "bike_id": bike_id,
                "user": user_id,
                "at": time.time(),
                "data": ev["data"],
            }
            _log_event(rec)
            fired.append(rec)
            if dispatch:
                await notify.for_event(rec, bike_name)  # local desktop alert
                for sub in webhooks.subscribers_for(ev["event"]):
                    deliveries.append((sub, rec))
    _save_state(state)
    # fan out deliveries concurrently so one slow endpoint can't stall the rest
    if deliveries:
        results = await asyncio.gather(
            *(webhooks.deliver(http, sub, rec) for sub, rec in deliveries))
        for (sub, rec), result in zip(deliveries, results):
            _log_event({"event": "webhook.delivery", "at": time.time(),
                        "target": sub["id"], "of": rec["event"], "result": result})
    return fired


async def poll_once(store: TokenStore, http: httpx.AsyncClient,
                    dispatch: bool = True) -> list[dict]:
    fired: list[dict] = []
    for user_id in store.users():
        try:
            fired += await poll_user(store, http, user_id, dispatch)
        except BoschError as e:
            _log_event({"event": "poll.error", "user": user_id,
                        "at": time.time(), "detail": e.detail})
    return fired


async def run_loop(app) -> None:
    """Long-running background task started from the app lifespan."""
    _prime_recent()
    while True:
        try:
            await poll_once(app.state.store, app.state.http)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            _log_event({"event": "poll.crash", "at": time.time(), "detail": str(e)})
        await asyncio.sleep(config.POLL_INTERVAL)


async def emit(http: httpx.AsyncClient, event: str, data: dict | None = None) -> dict:
    """Raise an event that didn't come from a bike-state diff.

    Manual triggers (the menu button) and schedules both land here, so they are
    logged and fanned out over exactly the same path as detected events.
    """
    rec = {"event": event, "at": time.time(), "data": data or {}, "source": "manual"}
    _log_event(rec)
    subs = webhooks.subscribers_for(event)
    results = await asyncio.gather(
        *(webhooks.deliver(http, sub, rec) for sub in subs)) if subs else []
    for sub, result in zip(subs, results):
        _log_event({"event": "webhook.delivery", "at": time.time(),
                    "target": sub["id"], "of": event, "result": result})
    return {"event": event, "delivered": len(subs),
            "ok": all(r.get("ok") for r in results) if results else True}


async def schedule_loop(app) -> None:
    """Fire scheduled triggers on wall-clock time, independent of bike polling."""
    while True:
        try:
            for item in schedules.due():
                await emit(app.state.http, item["event"],
                           {"source": "schedule", "schedule_id": item["id"],
                            "at": item["at"], "repeat": item["repeat"]})
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            _log_event({"event": "schedule.crash", "at": time.time(), "detail": str(e)})
        await asyncio.sleep(config.SCHEDULE_TICK)
