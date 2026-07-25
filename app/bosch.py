"""Async facade over Bosch's fragmented cloud (profile + activity hosts).

Hides OAuth, auto-refreshes tokens, caches per Bosch's cadence, and normalizes
the raw JSON into clean shapes the dashboard can consume directly.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from . import auth, config
from .store import TokenStore

# Best-effort assist-mode id -> display name (from observed activity data).
MODE_NAMES = {
    "0": "OFF",
    "A100M00040": "ECO",
    "A100M00030": "TOUR",
    "A100M0AUTO": "AUTO",
    "A100E00009": "CARGO",
}


class BoschError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class _Cache:
    """Tiny in-process TTL cache keyed by (user, url)."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], tuple[float, object]] = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    def put(self, key, value, ttl):
        self._d[key] = (time.monotonic() + ttl, value)


_CACHE = _Cache()


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class BoschClient:
    def __init__(self, store: TokenStore, user_id: str, http: httpx.AsyncClient,
                 fresh: bool = False):
        self.store = store
        self.user_id = user_id
        self.http = http
        self.fresh = fresh  # poller sets this to bypass the read cache

    # --- auth ---------------------------------------------------------------
    async def _token(self) -> str:
        rec = self.store.get(self.user_id)
        if not rec:
            raise BoschError(401, "unknown user / not logged in")
        if rec.get("expires_at", 0) - time.time() < 600:
            fresh = await auth.refresh_token(self.http, rec["refresh_token"])
            self.store.update(self.user_id, fresh)
            rec = self.store.get(self.user_id)
        return rec["access_token"]

    async def _get(self, base: str, path: str, ttl: int, params: dict | None = None):
        url = f"{base}{path}"
        key = (self.user_id, url + (f"?{params}" if params else ""))
        if not self.fresh:
            cached = _CACHE.get(key)
            if cached is not None:
                return cached
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        r = await self.http.get(url, headers=headers, params=params)
        if r.status_code == 401:  # refresh once and retry
            rec = self.store.get(self.user_id)
            fresh = await auth.refresh_token(self.http, rec["refresh_token"])
            self.store.update(self.user_id, fresh)
            headers["Authorization"] = f"Bearer {fresh['access_token']}"
            r = await self.http.get(url, headers=headers, params=params)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise BoschError(r.status_code, r.text[:200])
        data = r.json()
        _CACHE.put(key, data, ttl)
        return data

    # --- raw endpoints ------------------------------------------------------
    async def _profile_list(self):
        d = await self._get(config.HOST_PROFILE, "/v1/bike-profile", config.TTL_PROFILE)
        return (d or {}).get("data", [])

    async def _profile(self, bike_id: str):
        return await self._get(config.HOST_PROFILE, f"/v1/bike-profile/{bike_id}", config.TTL_PROFILE)

    async def _soc(self, bike_id: str):
        return await self._get(config.HOST_PROFILE, f"/v1/state-of-charge/{bike_id}", config.TTL_LIVE)

    async def _activities(self, bike_id: str | None):
        params = {"bikeId": bike_id} if bike_id else None
        d = await self._get(config.HOST_ACTIVITY, "/v1/activity", config.TTL_RIDES, params)
        return (d or {}).get("data", [])

    async def _activity_detail(self, aid: str):
        d = await self._get(config.HOST_ACTIVITY, f"/v1/activity/{aid}/detail", config.TTL_RIDE)
        return ((d or {}).get("data", {}) or {}).get("attributes", {}).get("activityData", []) or []

    # --- normalized ---------------------------------------------------------
    async def bikes(self) -> list[dict]:
        out = []
        for b in await self._profile_list():
            a = b.get("attributes", {})
            du = a.get("driveUnit") or {}
            out.append({
                "id": b.get("id"),
                "brand": a.get("brandName"),
                "model": du.get("modelId") or du.get("productName"),
                "drive_unit": du.get("productName"),
                "category": du.get("bikeCategory"),
            })
        return out

    async def battery(self, bike_id: str) -> dict:
        prof = await self._profile(bike_id)
        soc = await self._soc(bike_id)
        if not prof:
            raise BoschError(404, "bike not found")
        a = prof["data"]["attributes"]
        # A bike can carry more than one pack (e.g. Bosch DualBattery). Bosch lists
        # each pack separately under `batteries`; earlier this only read [0], which
        # under-reported capacity/lifetime and hid the second pack entirely.
        bats = a.get("batteries") or []
        bat = bats[0] if bats else {}          # for the SoC field fallbacks below
        du = a.get("driveUnit") or {}
        modes = du.get("driveUnitAssistModes") or []
        # range per mode: soc.reachableRange aligns with the non-off assist modes
        ranges = (soc or {}).get("reachableRange") or []
        named_modes = [m for m in modes if (m.get("reachableRange") or 0) > 0]
        range_per_mode = []
        for i, km in enumerate(ranges):
            mid = named_modes[i]["id"] if i < len(named_modes) else None
            range_per_mode.append({"mode": MODE_NAMES.get(mid, mid), "range_km": km})

        def _sum(field):
            vals = [b.get(field) for b in bats if b.get(field) is not None]
            return sum(vals) if vals else None

        # Per-pack detail; packs age independently, so cycles stay per-pack.
        packs = [{
            "product": b.get("productName"),
            "serial": b.get("serialNumber"),
            "capacity_wh": b.get("totalEnergy"),
            "level_percent": b.get("batteryLevel"),
            "charge_cycles": (b.get("numberOfFullChargeCycles") or {}).get("total"),
            "delivered_lifetime_wh": b.get("deliveredWhOverLifetime"),
        } for b in bats]

        return {
            "bike_id": bike_id,
            "live": soc is not None,
            # SoC endpoint reports a single system-level charge for the whole bike
            # (both packs combined); fall back to the first pack's static profile.
            "level_percent": (soc or {}).get("stateOfCharge", bat.get("batteryLevel")),
            "is_charging": (soc or {}).get("chargingActive", bat.get("isCharging")),
            "charger_connected": (soc or {}).get("chargerConnected", bat.get("isChargerConnected")),
            # Combined across packs. For a single-battery bike these equal that pack,
            # so existing bikes render exactly as before.
            "total_capacity_wh": _sum("totalEnergy"),
            "delivered_lifetime_wh": _sum("deliveredWhOverLifetime"),
            "charge_cycles": (bat.get("numberOfFullChargeCycles") or {}).get("total"),
            "battery_count": len(bats),
            "packs": packs,
            "remaining_energy_for_rider": (soc or {}).get("remainingEnergyForRider"),
            "remaining_charging_time": (soc or {}).get("remainingChargingTime"),
            "range_per_mode": range_per_mode,
            "odometer_km": round((soc or {}).get("odometer", du.get("totalDistanceTraveled") or 0) / 1000, 1),
            "last_update": (soc or {}).get("stateOfChargeLatestUpdate"),
        }

    async def debug_battery(self, bike_id: str) -> dict:
        """Raw pack + state-of-charge payloads, for confirming multi-battery shape.
        Not part of the dashboard — inspect via /api/bikes/<id>/debug/battery."""
        prof = await self._profile(bike_id)
        soc = await self._soc(bike_id)
        if not prof:
            raise BoschError(404, "bike not found")
        return {
            "battery_count": len(prof["data"]["attributes"].get("batteries") or []),
            "batteries_raw": prof["data"]["attributes"].get("batteries"),
            "state_of_charge_raw": soc,
        }

    async def profile(self, bike_id: str) -> dict:
        prof = await self._profile(bike_id)
        if not prof:
            raise BoschError(404, "bike not found")
        a = prof["data"]["attributes"]
        du = a.get("driveUnit") or {}
        cm = a.get("connectedModule") or {}
        components = []

        def add(slot, c):
            if c and c.get("productName"):
                components.append({
                    "slot": slot,
                    "product": c.get("productName"),
                    "firmware": c.get("softwareVersion"),
                    "serial": c.get("serialNumber"),
                    "manufactured": c.get("manufacturingDate"),
                })

        # Preserve the original slot order, but expand the single "battery" entry
        # into one row per pack so a dual-battery bike lists both.
        add("drive_unit", du)
        bats = a.get("batteries") or []
        for i, b in enumerate(bats):
            add("battery" if len(bats) == 1 else f"battery {i + 1}", b)
        add("connect_module", cm)
        add("abs", a.get("antiLockBrakeSystem") or {})
        add("display", a.get("headUnit") or {})
        add("remote", a.get("remoteControl") or {})
        pot = du.get("powerOnTime") or {}
        return {
            "bike_id": bike_id,
            "brand": a.get("brandName"),
            "model": du.get("modelId"),
            "category": du.get("bikeCategory"),
            "has_abs": bool(a.get("antiLockBrakeSystem")),
            "alarm_enabled": cm.get("isAlarmFeatureEnabled"),
            "lock_enabled": (du.get("lock") or {}).get("isEnabled"),
            "max_assist_speed_kmh": du.get("maxAssistanceSpeed"),
            "power_on_hours_total": pot.get("total"),
            "power_on_hours_motor": pot.get("withMotorSupport"),
            "tuning_detected": (du.get("tuningDetection") or {}).get("isDetected"),
            "components": components,
        }

    async def stats(self, bike_id: str) -> list[dict]:
        prof = await self._profile(bike_id)
        if not prof:
            raise BoschError(404, "bike not found")
        du = prof["data"]["attributes"].get("driveUnit") or {}
        out = []
        for m in du.get("driveUnitAssistModes") or []:
            st = m.get("statistics") or {}
            dist_km = (st.get("distance") or 0) / 1000
            wh = st.get("consumedEnergy") or 0
            out.append({
                "mode": MODE_NAMES.get(m.get("id"), m.get("id")),
                "distance_km": round(dist_km, 1),
                "energy_wh": wh,
                "wh_per_km": round(wh / dist_km, 2) if dist_km > 0 else None,
            })
        out.sort(key=lambda x: x["distance_km"], reverse=True)
        return out

    def _ride_summary(self, act: dict) -> dict:
        a = act["attributes"]
        dur = a.get("durationWithoutStops") or 0
        return {
            "id": act["id"],
            "start": _iso(a.get("startTime")),
            "end": _iso(a.get("endTime")),
            "title": a.get("title"),
            "distance_km": round((a.get("distance") or 0) / 1000, 2),
            "duration_min": round(dur / 60, 1),
            "avg_speed_kmh": a.get("averageSpeed"),
            "max_speed_kmh": a.get("maximumSpeed"),
            "avg_power_w": a.get("averageRiderPower"),
            "max_power_w": a.get("maximumRiderPower"),
            "calories": a.get("caloriesBurnt"),
            "co2_saved_g": a.get("co2EmissionsCarEquivalentGrams"),
            "abs_interventions": (a.get("brakeEvents") or {}).get("amountOfAbsInterventionEvents"),
            "rider_energy_share": a.get("riderEnergyShare"),
            "elevation_gain_m": a.get("elevationGain"),
            "elevation_loss_m": a.get("elevationLoss"),
            "avg_cadence": a.get("averageCadence"),
        }

    async def rides(self, bike_id: str, with_gps: bool = False) -> list[dict]:
        acts = await self._activities(bike_id)
        acts.sort(key=lambda x: x["attributes"].get("startTime", 0), reverse=True)
        out = [self._ride_summary(a) for a in acts]
        if with_gps and acts:
            # token is already fresh (fetched above), so these fan out safely
            details = await asyncio.gather(*(self._activity_detail(a["id"]) for a in acts))
            for row, samples in zip(out, details):
                n = sum(1 for p in samples if p.get("lat") is not None)
                row["has_gps"] = n > 0
                row["gps_points"] = n
        return out

    def _track_points(self, samples: list, start: int | None, end: int | None) -> list[dict]:
        pts = [p for p in samples if p.get("lat") is not None and p.get("lon") is not None]
        n = len(pts)
        span = (end - start) if (start and end and n > 1) else 0
        out = []
        for i, p in enumerate(pts):
            t = start + int(span * i / (n - 1)) if span else start
            out.append({
                "lat": p["lat"], "lon": p["lon"],
                "time": _iso(t),
                "speed_kmh": p.get("v"),
                "cadence": p.get("c"),
                "power_w": p.get("p"),
                "elevation_m": p.get("h"),
            })
        return out

    async def ride(self, bike_id: str, aid: str) -> dict:
        acts = await self._activities(bike_id)
        act = next((a for a in acts if a["id"] == aid), None)
        if not act:
            raise BoschError(404, "ride not found")
        samples = await self._activity_detail(aid)
        a = act["attributes"]
        summary = self._ride_summary(act)
        summary["track"] = self._track_points(samples, a.get("startTime"), a.get("endTime"))
        summary["has_gps"] = len(summary["track"]) > 0
        return summary

    async def last_location(self, bike_id: str) -> dict | None:
        """Newest ride that carries GPS -> its final fix (best available position)."""
        acts = await self._activities(bike_id)
        acts.sort(key=lambda x: x["attributes"].get("startTime", 0), reverse=True)
        for act in acts:
            a = act["attributes"]
            track = self._track_points(await self._activity_detail(act["id"]),
                                       a.get("startTime"), a.get("endTime"))
            if track:
                last = track[-1]
                return {
                    "lat": last["lat"], "lon": last["lon"],
                    "time": last["time"],
                    "source": f"ride:{act['id']}",
                    "note": "last-known from most recent GPS-recorded ride",
                }
        return None
