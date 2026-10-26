"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Environment variable {name} must be an integer, got {raw!r}") from None


def _port_range(raw: str) -> tuple[int, int]:
    raw = raw.strip()
    try:
        if "-" in raw:
            start, end = (int(part) for part in raw.split("-", 1))
        else:
            start = end = int(raw)
    except ValueError:
        raise SystemExit(f"PORT_RANGE must look like 38281-38300, got {raw!r}") from None
    if not (1 <= start <= end <= 65535):
        raise SystemExit(f"PORT_RANGE {raw!r} is not a valid port range")
    return start, end


@dataclass(frozen=True)
class Config:
    data_dir: Path
    ap_dir: Path
    ap_python: str
    helper: Path
    server_launcher: Path  # starts a game's MultiServer.py with its passwords from the console (ap_server.py)
    port_start: int
    port_end: int
    public_host: str
    admin_user: str
    admin_password: str
    max_upload_bytes: int
    max_extract_bytes: int
    generation_timeout: int
    # Where Archipelago itself keeps its per-user data (host.yaml, logs, custom worlds when the install is read-only).
    ap_data_home: Path
    # Look for newer Archipelago releases on GitHub (once at start, then twice a day) so admins know to update.
    update_check: bool
    releases_url: str
    # Where further Archipelago versions are downloaded from (versions.py); {version} is the release, such as 0.6.2
    ap_source_url: str
    # GitHub's API, for custom games that come from a repository's releases (replaced by a fake in tests)
    github_api: str
    # archipelago.gg, where games are taken over from (ggimport.py; replaced by a fake in tests)
    archipelago_gg: str
    # Discord's OAuth2 endpoints, for signing in with Discord (replaced by a fake in tests)
    discord_api: str
    discord_authorize: str
    # How long a minute of the sleep timer lasts (rooms.py): seconds in tests
    sleep_minute_seconds: int
    # How long a day lasts for reminding players who stopped playing (reminders.py): seconds in tests
    reminder_day_seconds: int
    # Certificate and key for encrypted game connections (tls.py), until an admin sets others in the web UI
    tls_cert: str
    tls_key: str

    # --- derived paths -------------------------------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "manager.sqlite3"

    @property
    def configs_dir(self) -> Path:
        return self.data_dir / "configs"

    @property
    def games_dir(self) -> Path:
        return self.data_dir / "games"

    @property
    def apworlds_dir(self) -> Path:
        return self.data_dir / "apworlds"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.configs_dir, self.games_dir, self.apworlds_dir, self.jobs_dir,
                     self.templates_dir, self.tmp_dir, self.ap_data_home):
            path.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    data_dir = Path(os.environ.get("DATA_DIR", "/config")).resolve()
    start, end = _port_range(os.environ.get("PORT_RANGE", "38281-38300"))
    return Config(
        data_dir=data_dir,
        ap_dir=Path(os.environ.get("AP_DIR", "/opt/archipelago")).resolve(),
        ap_python=os.environ.get("AP_PYTHON", "/opt/venv-ap/bin/python"),
        helper=Path(__file__).resolve().parent.parent / "ap_helper.py",
        server_launcher=Path(__file__).resolve().parent.parent / "ap_server.py",
        port_start=start,
        port_end=end,
        public_host=os.environ.get("PUBLIC_HOST", "").strip(),
        admin_user=os.environ.get("ADMIN_USER", "").strip(),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        max_upload_bytes=_int("MAX_UPLOAD_MB", 512) * 1024 * 1024,
        max_extract_bytes=_int("MAX_EXTRACT_MB", 2048) * 1024 * 1024,
        generation_timeout=_int("GENERATION_TIMEOUT", 3600),
        ap_data_home=Path(os.environ.get("XDG_DATA_HOME") or (data_dir / "ap-data")).resolve(),
        update_check=os.environ.get("UPDATE_CHECK", "true").strip().lower() not in ("0", "false", "no", "off"),
        releases_url=os.environ.get("AP_RELEASES_URL",
                                    "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"),
        ap_source_url=os.environ.get(
            "AP_SOURCE_URL", "https://github.com/ArchipelagoMW/Archipelago/archive/refs/tags/{version}.tar.gz"),
        github_api=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        archipelago_gg=os.environ.get("ARCHIPELAGO_GG_URL", "https://archipelago.gg").rstrip("/"),
        discord_api=os.environ.get("DISCORD_API_URL", "https://discord.com/api/v10").rstrip("/"),
        discord_authorize=os.environ.get("DISCORD_AUTHORIZE_URL", "https://discord.com/oauth2/authorize"),
        sleep_minute_seconds=max(1, _int("SLEEP_MINUTE_SECONDS", 60)),
        reminder_day_seconds=max(1, _int("REMINDER_DAY_SECONDS", 86400)),
        tls_cert=os.environ.get("TLS_CERT", "").strip(),
        tls_key=os.environ.get("TLS_KEY", "").strip(),
    )


CONFIG = load_config()

if not CONFIG.ap_dir.is_dir() and "pytest" not in sys.modules:
    print(f"warning: AP_DIR {CONFIG.ap_dir} does not exist; Archipelago features will not work", file=sys.stderr)
