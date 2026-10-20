"""Global manager settings and validation of Archipelago server options."""
from __future__ import annotations

from typing import Any

from . import db
from .config import CONFIG

RELEASE_MODES = ("auto", "enabled", "disabled", "goal", "auto-enabled")
REMAINING_MODES = ("enabled", "disabled", "goal")
COUNTDOWN_MODES = ("enabled", "disabled", "auto")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

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
}

_BOOL_KEYS = {"allow_registration", "users_can_start_rooms", "users_can_generate", "users_can_upload_apworlds",
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


def get_settings() -> dict[str, Any]:
    stored = {row["key"]: db.jloads(row["value"]) for row in db.query("SELECT key, value FROM settings")}
    result = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in stored:
            result[key] = stored[key]
    result["default_server_options"] = clean_server_options(stored.get("default_server_options") or {})
    return result


def get_setting(key: str) -> Any:
    return get_settings()[key]


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
        clean[key] = value
    with db.connection() as conn:
        for key, value in clean.items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, db.jdumps(value)))
    return get_settings()
