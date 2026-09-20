"""Small shared prefs file: units for the UI, charge ceiling for the poller.

One JSON file read and written by the dashboard, the menu bar app and the
poller, so a change in any of them is visible to the others. Writes merge, so a
client that only knows about `units` cannot clobber `charge_target`.
"""
from __future__ import annotations

import json
from typing import Any

from . import config

VALID_UNITS = {"metric", "imperial"}


def _raw() -> dict:
    p = config.data_path(config.PREFS_FILE)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def clean_target(value: Any) -> int | None:
    """Coerce a charge ceiling, or None if it is not a sane percentage."""
    try:
        pct = int(value)
    except (TypeError, ValueError):
        return None
    if not (config.CHARGE_TARGET_MIN <= pct <= config.CHARGE_TARGET_MAX):
        return None
    return pct


def read() -> dict:
    data = _raw()
    units = data.get("units")
    return {
        "units": units if units in VALID_UNITS else "metric",
        "charge_target": clean_target(data.get("charge_target"))
                         or config.BATTERY_TARGET_PCT,
    }


def write(units: str | None = None, charge_target: Any = None) -> dict:
    """Merge the given fields into the file and return the full prefs."""
    data = _raw()
    if units is not None:
        data["units"] = units
    if charge_target is not None:
        data["charge_target"] = charge_target
    config.data_path(config.PREFS_FILE).write_text(json.dumps(data, indent=2))
    return read()


def charge_target() -> int:
    return read()["charge_target"]
