"""Time-based manual triggers: fire an event at HH:MM, once or daily.

Kept separate from the bike poller: these fire on wall-clock time and never
touch Bosch's API, so they keep working while the network or the account is
down. Times are local to this machine, which is the only clock a user sees.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import config


def _path() -> Path:
    return config.data_path(config.SCHEDULES_FILE)


def _read() -> list[dict]:
    p = _path()
    return json.loads(p.read_text()) if p.exists() else []


def _write(items: list[dict]) -> None:
    _path().write_text(json.dumps(items, indent=2))


def _parse_hhmm(text: str) -> tuple[int, int]:
    hh, _, mm = text.partition(":")
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("time must be within 00:00-23:59")
    return h, m


def next_run_after(hhmm: str, now: float | None = None) -> float:
    """Epoch seconds of the next local occurrence of HH:MM, strictly future."""
    h, m = _parse_hhmm(hhmm)
    base = datetime.fromtimestamp(now if now is not None else time.time())
    run = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if run <= base:
        run += timedelta(days=1)
    return run.timestamp()


def list_all() -> list[dict]:
    return _read()


def add(event: str, at: str, repeat: str = "once") -> dict:
    if event not in config.MANUAL_EVENTS:
        raise ValueError(f"event must be one of {', '.join(config.MANUAL_EVENTS)}")
    if repeat not in ("once", "daily"):
        raise ValueError("repeat must be 'once' or 'daily'")
    item = {
        "id": uuid.uuid4().hex[:12],
        "event": event,
        "at": at,                      # "HH:MM", local time
        "repeat": repeat,
        "next_run": next_run_after(at),
        "active": True,
        "created": time.time(),
    }
    items = _read()
    items.append(item)
    _write(items)
    return item


def delete(sched_id: str) -> bool:
    items = _read()
    kept = [s for s in items if s["id"] != sched_id]
    if len(kept) == len(items):
        return False
    _write(kept)
    return True


def due(now: float | None = None) -> list[dict]:
    """Schedules whose time has passed, advancing or retiring each one.

    A machine asleep past a firing time fires once on wake, not once per missed
    day: 'daily' jumps to the next future slot rather than replaying the gap.
    """
    now = now if now is not None else time.time()
    items = _read()
    fired, kept, changed = [], [], False
    for s in items:
        if s.get("active") and s.get("next_run", 0) <= now:
            fired.append(s)
            changed = True
            if s["repeat"] == "daily":
                s["next_run"] = next_run_after(s["at"], now)
                kept.append(s)
            # "once" is dropped: it has done its job
        else:
            kept.append(s)
    if changed:
        _write(kept)
    return fired
