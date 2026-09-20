"""Event detection: diff two bike snapshots into edge-triggered events.

Pure functions — no I/O — so the rules are easy to test.
"""
from __future__ import annotations

from . import config


def build_snapshot(battery: dict, profile: dict, rides: list[dict]) -> dict:
    """Condense the normalized data into the fields events are derived from."""
    latest = rides[0] if rides else None
    return {
        "level": battery.get("level_percent"),
        "charging": battery.get("is_charging"),
        "plugged": battery.get("charger_connected"),
        "odometer_km": battery.get("odometer_km"),
        "latest_ride_id": latest["id"] if latest else None,
        "latest_ride": latest,
        "firmware": {c["slot"]: c["firmware"] for c in profile.get("components", [])},
        "live": battery.get("live"),
    }


def _rose(prev, cur) -> bool:
    return prev is False and cur is True


def _fell(prev, cur) -> bool:
    return prev is True and cur is False


def detect(prev: dict, cur: dict) -> list[dict]:
    """Return the list of events implied by prev -> cur. Edge-triggered."""
    out: list[dict] = []

    def emit(name, data):
        out.append({"event": name, "data": data})

    # --- charging / charger ------------------------------------------------
    if _rose(prev.get("charging"), cur.get("charging")):
        emit("battery.charging_started", {"level": cur.get("level")})
    if _fell(prev.get("charging"), cur.get("charging")):
        emit("battery.charging_stopped", {"level": cur.get("level")})
    if _rose(prev.get("plugged"), cur.get("plugged")):
        emit("charger.connected", {"level": cur.get("level")})
    if _fell(prev.get("plugged"), cur.get("plugged")):
        emit("charger.disconnected", {"level": cur.get("level")})

    # --- battery level thresholds ------------------------------------------
    pl, cl = prev.get("level"), cur.get("level")
    if isinstance(pl, (int, float)) and isinstance(cl, (int, float)):
        if cl >= config.BATTERY_FULL_PCT > pl:
            emit("battery.full", {"level": cl})
        target = config.BATTERY_TARGET_PCT
        if cl >= target > pl:
            emit("battery.target", {"level": cl, "target": target})
        low = config.BATTERY_LOW_PCT
        if pl > low >= cl:
            emit("battery.low", {"level": cl, "threshold": low})

    # --- ride completed (a new activity appeared) --------------------------
    cur_ride = cur.get("latest_ride_id")
    if cur_ride and cur_ride != prev.get("latest_ride_id"):
        emit("ride.completed", cur.get("latest_ride") or {"id": cur_ride})

    # --- firmware changes ---------------------------------------------------
    pf, cf = prev.get("firmware") or {}, cur.get("firmware") or {}
    for slot, ver in cf.items():
        old = pf.get(slot)
        if old and old != ver:
            emit("firmware.changed", {"component": slot, "from": old, "to": ver})

    return out
