"""Global manager settings and validation of Archipelago server options."""
from __future__ import annotations

import re
from typing import Any

from . import db
from .config import CONFIG

RELEASE_MODES = ("auto", "enabled", "disabled", "goal", "auto-enabled")
REMAINING_MODES = ("enabled", "disabled", "goal")
COUNTDOWN_MODES = ("enabled", "disabled", "auto")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
# Kinds of Discord notifications an admin can switch on and off
NOTIFY_EVENTS = ("game_online", "game_crashed", "player_goal", "player_stuck", "slot_handover", "hint_found", "generation",
                 "signup", "apworld_pending", "apworld_update", "ap_update")
_URL = re.compile(r"^https?://[^\s/?#]+[^\s]*$")

# Per-game options handed to MultiServer.py. auto_shutdown is stored in minutes (the server takes seconds).
DEFAULT_SERVER_OPTIONS: dict[str, Any] = {
    "hint_cost": 10,
    "location_check_points": 1,
    "release_mode": "auto",
    "collect_mode": "auto",
    "remaining_mode": "goal",
    "countdown_mode": "auto",
    "auto_shutdown_minutes": 0,
    "disable_item_cheat": False,
    "compatibility": 2,
    "use_embedded_options": False,
    "loglevel": "info",
}

DEFAULTS: dict[str, Any] = {
    "public_host": CONFIG.public_host,
    "allow_registration": False,
    "users_can_start_rooms": True,
    "users_can_generate": False,
    "users_can_upload_apworlds": True,
    "auto_restart_crashed": True,
    "default_server_options": DEFAULT_SERVER_OPTIONS,
    "discord_webhook_url": "",
    "notify_events": list(NOTIFY_EVENTS),
    "notify_language": "en",
    "notify_mentions": True,  # players who linked Discord may ask to be mentioned (notify.PING_KINDS)
    # Address of this web UI as players open it: links in notifications, and where Discord sends people back to
    "site_url": "",
    "discord_login": False,
    "discord_client_id": "",
    "discord_client_secret": "",  # never sent to the browser; see public_settings()
    "discord_signup": False,       # create accounts for Discord users nobody linked yet
    "discord_guild_id": "",        # if set, only members of this Discord server may sign in with Discord
}
LANGUAGES = ("en", "de")

_BOOL_KEYS = {"discord_login", "discord_signup", "notify_mentions",
              "allow_registration", "users_can_start_rooms", "users_can_generate", "users_can_upload_apworlds",
              "auto_restart_crashed"}


class SettingsError(ValueError):
    pass


def clean_server_options(options: dict[str, Any] | None, *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validates and merges server options over base (the defaults if not given). Unknown keys are rejected."""
    result = dict(base if base is not None else DEFAULT_SERVER_OPTIONS)
    for key, value in (options or {}).items():
        if key not in DEFAULT_SERVER_OPTIONS:
            raise SettingsError(f"Unknown server option: {key}")
        default = DEFAULT_SERVER_OPTIONS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise SettingsError(f"{key} must be true or false")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingsError(f"{key} must be a whole number")
        elif not isinstance(value, str):
            raise SettingsError(f"{key} must be text")
        result[key] = value

    def choice(key: str, allowed: tuple[str, ...]) -> None:
        if result[key] not in allowed:
            raise SettingsError(f"{key} must be one of: {', '.join(allowed)}")

    choice("release_mode", RELEASE_MODES)
    choice("collect_mode", RELEASE_MODES)
    choice("remaining_mode", REMAINING_MODES)
    choice("countdown_mode", COUNTDOWN_MODES)
    choice("loglevel", LOG_LEVELS)
    if result["compatibility"] not in (0, 1, 2):
        raise SettingsError("compatibility must be 0, 1 or 2")
    for key, upper in (("hint_cost", 100), ("location_check_points", 1000), ("auto_shutdown_minutes", 60 * 24 * 30)):
        if not (0 <= result[key] <= upper):
            raise SettingsError(f"{key} must be between 0 and {upper}")
    return result


def valid_url(value: str) -> bool:
    return len(value) <= 512 and bool(_URL.match(value))


def get_settings() -> dict[str, Any]:
    stored = {row["key"]: db.jloads(row["value"]) for row in db.query("SELECT key, value FROM settings")}
    result = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in stored:
            result[key] = stored[key]
    result["default_server_options"] = clean_server_options(stored.get("default_server_options") or {})
    if "site_url" not in stored and stored.get("notify_link_url"):  # its name up to version 1.2
        result["site_url"] = stored["notify_link_url"]
    return result


def public_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Settings as an admin's browser may see them: without the Discord client secret, only whether one is set."""
    return {**{k: v for k, v in values.items() if k != "discord_client_secret"},
            "discord_client_secret_set": bool(values["discord_client_secret"])}


def discord_login_ready(values: dict[str, Any] | None = None) -> bool:
    values = values or get_settings()
    return bool(values["discord_login"] and values["discord_client_id"] and values["discord_client_secret"]
                and values["site_url"])


def get_setting(key: str) -> Any:
    return get_settings()[key]


# How the settings below are called in the web UI, for error messages
_LABELS = {"discord_webhook_url": "The webhook address", "site_url": "The address of this web UI",
           "discord_client_id": "The client ID", "discord_guild_id": "The server ID"}


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            raise SettingsError(f"Unknown setting: {key}")
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise SettingsError(f"{key} must be true or false")
        elif key == "public_host":
            if not isinstance(value, str) or len(value) > 255 or any(c in value for c in " /\\\t\n"):
                raise SettingsError("public_host must be a host name or IP address, e.g. archipelago.example.com")
            value = value.strip()
        elif key == "default_server_options":
            value = clean_server_options(value, base=get_settings()["default_server_options"])
        elif key in ("discord_webhook_url", "site_url"):
            value = value.strip() if isinstance(value, str) else value
            if not isinstance(value, str) or (value and not valid_url(value)):
                raise SettingsError(f"{_LABELS[key]} must start with http:// or https://")
            if key == "site_url":
                value = value.rstrip("/")
        elif key in ("discord_client_id", "discord_guild_id"):
            value = value.strip() if isinstance(value, str) else value
            if not isinstance(value, str) or (value and not re.fullmatch(r"\d{5,25}", value)):
                raise SettingsError(f"{_LABELS[key]} must be the number Discord shows for it")
        elif key == "discord_client_secret":
            if not isinstance(value, str) or len(value) > 200 or not re.fullmatch(r"[\x21-\x7e]*", value):
                raise SettingsError("That is not a valid client secret")
        elif key == "notify_language":
            if value not in LANGUAGES:
                raise SettingsError(f"notify_language must be one of: {', '.join(LANGUAGES)}")
        elif key == "notify_events":
            if not isinstance(value, list) or any(v not in NOTIFY_EVENTS for v in value):
                raise SettingsError(f"notify_events may only contain: {', '.join(NOTIFY_EVENTS)}")
            value = [e for e in NOTIFY_EVENTS if e in value]
        clean[key] = value
    after = {**get_settings(), **clean}
    if after["discord_login"] and not discord_login_ready(after):
        raise SettingsError("To turn on signing in with Discord, enter the client ID, the client secret and the "
                            "address of this web UI")
    with db.connection() as conn:
        for key, value in clean.items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, db.jdumps(value)))
    return get_settings()


# Values the manager remembers for itself (such as which update it already announced). They share the settings table
# but are not settings: the API neither shows nor accepts them.
def get_state(key: str, default: Any = None) -> Any:
    row = db.query_one("SELECT value FROM settings WHERE key = ?", (f"state:{key}",))
    return db.jloads(row["value"], default) if row else default


def set_state(key: str, value: Any) -> None:
    db.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (f"state:{key}", db.jdumps(value)))
