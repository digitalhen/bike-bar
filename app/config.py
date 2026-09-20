"""Static configuration for the Bosch eBike Flow facade."""

# --- OAuth (Bosch Keycloak, realm 'obc') ------------------------------------
AUTH_URL = "https://p9.authz.bosch.com/auth/realms/obc/protocol/openid-connect/auth"
TOKEN_URL = "https://p9.authz.bosch.com/auth/realms/obc/protocol/openid-connect/token"
CLIENT_ID = "one-bike-app"
REDIRECT_URI = "onebikeapp-ios://com.bosch.ebike.onebikeapp/oauth2redirect"
SCOPE = "openid offline_access"

# --- Backend service hosts (all <service>.prod.connected-biking.cloud) -------
HOST_PROFILE = "https://obc-rider-profile.prod.connected-biking.cloud"
HOST_ACTIVITY = "https://obc-rider-activity.prod.connected-biking.cloud"

# --- Cache TTLs (seconds) — respect Bosch's ~5-min ConnectModule cadence ----
TTL_LIVE = 300      # state-of-charge / battery
TTL_PROFILE = 900   # hardware/firmware/per-mode stats (rarely change)
TTL_RIDES = 600     # activity list
TTL_RIDE = 86400    # a single ride's detail is immutable once recorded

# --- Storage ----------------------------------------------------------------
USERS_FILE = "bosch_users.json"        # multi-user token store
LEGACY_TOKEN_FILE = "bosch_tokens.json"  # imported on first run if present
WEBHOOKS_FILE = "bosch_webhooks.json"  # outbound webhook subscriptions
POLLER_STATE_FILE = "bosch_poller_state.json"  # last-known snapshot per bike
EVENTS_LOG_FILE = "bosch_events.jsonl"  # append-only event log
PREFS_FILE = "bosch_prefs.json"  # shared UI prefs (units) synced across web + app
SCHEDULES_FILE = "bosch_schedules.json"  # time-based manual triggers

# Where those files live. Defaults to the repo root (dev), but the packaged
# menu bar app sets BOSCH_FLOW_DATA_DIR to ~/Library/Application Support/Bosch
# Flow, since a signed .app bundle is read-only.
import os as _os
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent


def data_dir() -> _Path:
    override = _os.environ.get("BOSCH_FLOW_DATA_DIR")
    d = _Path(override).expanduser() if override else _REPO_ROOT
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_path(filename: str) -> _Path:
    return data_dir() / filename

# --- Event poller -----------------------------------------------------------
POLL_INTERVAL = 300     # seconds between poll cycles
BATTERY_LOW_PCT = 20    # threshold for the battery.low event
BATTERY_FULL_PCT = 100  # level considered "full"
# Partial-charge target. Crossing this upward fires battery.target, which is the
# hook for "stop at 80%" style automations that spare the pack a full top-off.
BATTERY_TARGET_PCT = 80
EVENTS_LOG_MAX = 500    # recent events kept for GET /api/events
WEBHOOK_TIMEOUT = 10
WEBHOOK_RETRIES = 3

# --- Scheduled triggers -----------------------------------------------------
SCHEDULE_TICK = 30      # seconds between due-schedule checks
# Events a client may raise by hand (menu button or a schedule). Deliberately a
# closed set: /api/trigger must not become a way to forge bike telemetry.
MANUAL_EVENTS = ("charge.requested", "charge.stopped")

# --- Local desktop notifications (macOS) ------------------------------------
# Off by default now: the "Bosch Bar" menu bar app posts native notifications
# by reading /api/events. Set True to also fire terminal-notifier from the server.
NOTIFY_ENABLED = False
# Attribute notifications to a real app so they render fully native. Point this
# at a dedicated "Bosch Bar" app bundle id once one exists; Terminal until then.
NOTIFY_SENDER = "com.apple.Terminal"
