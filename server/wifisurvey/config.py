"""Configuration loading for the Wi-Fi Survey server.

Settings come from, in order of increasing precedence:

1. built-in defaults (``0.0.0.0:8800``, ``/var/lib/wifisurvey`` for root installs,
   ``$XDG_DATA_HOME/wifisurvey`` otherwise),
2. a TOML file (``/etc/wifisurvey/config.toml`` for system installs,
   ``$XDG_CONFIG_HOME/wifisurvey/config.toml`` for user installs),
3. environment variables (``WIFISURVEY_*``),
4. explicit overrides passed by tests or the CLI.

Secrets are generated once by the installer (or by ``wifi-survey init``) and
never derived from host names or addresses.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import socket
import stat
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

APP_NAME = "wifisurvey"
CONFIG_ENV = "WIFISURVEY_CONFIG"
SYSTEM_CONFIG_PATH = Path("/etc/wifisurvey/config.toml")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8800
DEFAULT_SESSION_TTL_HOURS = 720
DEFAULT_PAIRING_CODE_TTL_S = 600


class ConfigError(RuntimeError):
    """Raised when a configuration file or value cannot be used."""


def xdg_config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value) if value else Path.home() / ".config"


def xdg_data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value) if value else Path.home() / ".local" / "share"


def default_config_path() -> Path:
    """Pick the config file this process should read/write by default."""
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    if os.geteuid() == 0:
        return SYSTEM_CONFIG_PATH
    return xdg_config_home() / APP_NAME / "config.toml"


def default_data_dir() -> Path:
    if os.geteuid() == 0:
        return Path("/var/lib/wifisurvey")
    return xdg_data_home() / APP_NAME


def system_timezone() -> str:
    """Best-effort IANA name of the host timezone."""
    env = os.environ.get("TZ")
    if env:
        return env
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"


@dataclass(frozen=True)
class ServerSection:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timezone: str = field(default_factory=system_timezone)
    data_dir: str = field(default_factory=lambda: str(default_data_dir()))
    static_dir: str = ""


@dataclass(frozen=True)
class SecuritySection:
    viewer_token: str = ""
    pairing_secret: str = ""
    session_ttl_hours: int = DEFAULT_SESSION_TTL_HOURS
    pairing_code_ttl_s: int = DEFAULT_PAIRING_CODE_TTL_S


@dataclass(frozen=True)
class UnifiSection:
    enabled: bool = False
    url: str = ""
    username: str = ""
    password_file: str = ""
    site: str = "default"
    verify_tls: bool = False


@dataclass(frozen=True)
class Settings:
    server: ServerSection = field(default_factory=ServerSection)
    security: SecuritySection = field(default_factory=SecuritySection)
    unifi: UnifiSection = field(default_factory=UnifiSection)
    config_path: str = ""

    # -- derived paths ---------------------------------------------------
    @property
    def db_path(self) -> str:
        return str(Path(self.server.data_dir) / "survey.db")

    @property
    def models_dir(self) -> str:
        return str(Path(self.server.data_dir) / "models")

    @property
    def static_dir(self) -> str:
        if self.server.static_dir:
            return self.server.static_dir
        return str(Path(__file__).resolve().parent.parent / "static")

    @property
    def session_secret(self) -> bytes:
        return (self.security.pairing_secret + ":session").encode("utf-8")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def load_settings(
    path: str | Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    data_dir: str | None = None,
    timezone: str | None = None,
    viewer_token: str | None = None,
    pairing_secret: str | None = None,
    static_dir: str | None = None,
) -> Settings:
    """Read the config file and apply environment/argument overrides."""
    config_path = Path(path).expanduser() if path else default_config_path()
    data = _read_toml(config_path)

    raw_server = _section(data, "server")
    raw_security = _section(data, "security")
    raw_unifi = _section(data, "unifi")

    server = ServerSection(
        host=str(raw_server.get("host", DEFAULT_HOST)),
        port=int(raw_server.get("port", DEFAULT_PORT)),
        timezone=str(raw_server.get("timezone") or system_timezone()),
        data_dir=str(raw_server.get("data_dir") or default_data_dir()),
        static_dir=str(raw_server.get("static_dir") or ""),
    )
    security = SecuritySection(
        viewer_token=str(raw_security.get("viewer_token") or ""),
        pairing_secret=str(raw_security.get("pairing_secret") or ""),
        session_ttl_hours=int(raw_security.get("session_ttl_hours", DEFAULT_SESSION_TTL_HOURS)),
        pairing_code_ttl_s=int(
            raw_security.get("pairing_code_ttl_s", DEFAULT_PAIRING_CODE_TTL_S)
        ),
    )
    unifi = UnifiSection(
        enabled=bool(raw_unifi.get("enabled", False)),
        url=str(raw_unifi.get("url") or ""),
        username=str(raw_unifi.get("username") or ""),
        password_file=str(raw_unifi.get("password_file") or ""),
        site=str(raw_unifi.get("site") or "default"),
        verify_tls=bool(raw_unifi.get("verify_tls", False)),
    )

    server = replace(
        server,
        host=_env("WIFISURVEY_HOST", host if host is not None else server.host) or DEFAULT_HOST,
        port=_env_int(
            "WIFISURVEY_PORT", port if port is not None else server.port
        ),
        timezone=_env("WIFISURVEY_TIMEZONE", timezone if timezone is not None else server.timezone)
        or system_timezone(),
        data_dir=_env(
            "WIFISURVEY_DATA_DIR", data_dir if data_dir is not None else server.data_dir
        )
        or str(default_data_dir()),
        static_dir=_env(
            "WIFISURVEY_STATIC_DIR", static_dir if static_dir is not None else server.static_dir
        )
        or "",
    )
    security = replace(
        security,
        viewer_token=_env(
            "WIFISURVEY_VIEWER_TOKEN",
            viewer_token if viewer_token is not None else security.viewer_token,
        )
        or "",
        pairing_secret=_env(
            "WIFISURVEY_PAIRING_SECRET",
            pairing_secret if pairing_secret is not None else security.pairing_secret,
        )
        or "",
        session_ttl_hours=_env_int("WIFISURVEY_SESSION_TTL_HOURS", security.session_ttl_hours),
        pairing_code_ttl_s=_env_int(
            "WIFISURVEY_PAIRING_CODE_TTL_S", security.pairing_code_ttl_s
        ),
    )
    unifi = replace(
        unifi,
        enabled=_env_bool("WIFISURVEY_UNIFI_ENABLED", unifi.enabled),
        url=_env("WIFISURVEY_UNIFI_URL", unifi.url) or "",
        username=_env("WIFISURVEY_UNIFI_USERNAME", unifi.username) or "",
        password_file=_env("WIFISURVEY_UNIFI_PASSWORD_FILE", unifi.password_file) or "",
        site=_env("WIFISURVEY_UNIFI_SITE", unifi.site) or "default",
        verify_tls=_env_bool("WIFISURVEY_UNIFI_VERIFY_TLS", unifi.verify_tls),
    )

    if not (0 < server.port < 65536):
        raise ConfigError(f"server.port must be 1..65535, got {server.port}")
    if not server.host:
        raise ConfigError("server.host must not be empty")

    return Settings(
        server=server,
        security=security,
        unifi=unifi,
        config_path=str(config_path),
    )


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config(settings: Settings) -> str:
    """Serialise settings to the documented TOML layout (stable order)."""
    lines = [
        "# Wi-Fi Survey configuration. Restart the service after editing.",
        "",
        "[server]",
        f"host = {_toml_quote(settings.server.host)}",
        f"port = {settings.server.port}",
        f"timezone = {_toml_quote(settings.server.timezone)}",
        f"data_dir = {_toml_quote(settings.server.data_dir)}",
        "",
        "[security]",
        f"viewer_token = {_toml_quote(settings.security.viewer_token)}",
        f"pairing_secret = {_toml_quote(settings.security.pairing_secret)}",
        f"session_ttl_hours = {settings.security.session_ttl_hours}",
        f"pairing_code_ttl_s = {settings.security.pairing_code_ttl_s}",
        "",
        "[unifi]",
        f"enabled = {'true' if settings.unifi.enabled else 'false'}",
        f"url = {_toml_quote(settings.unifi.url)}",
        f"username = {_toml_quote(settings.unifi.username)}",
        f"password_file = {_toml_quote(settings.unifi.password_file)}",
        f"site = {_toml_quote(settings.unifi.site)}",
        f"verify_tls = {'true' if settings.unifi.verify_tls else 'false'}",
        "",
    ]
    return "\n".join(lines)


def _write_private(path: Path, text: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def generate_secret(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


def ensure_config(path: str | Path | None = None, **overrides: Any) -> Settings:
    """Load config, creating it with fresh secrets when absent.

    Existing tokens are preserved: re-running this must never invalidate a
    paired browser or node.
    """
    target = Path(path).expanduser() if path else default_config_path()
    existed = target.exists()
    settings = load_settings(target, **overrides)
    changed = False
    security = settings.security
    if not security.viewer_token:
        security = replace(security, viewer_token=generate_secret(24))
        changed = True
    if not security.pairing_secret:
        security = replace(security, pairing_secret=generate_secret(32))
        changed = True
    if changed or not existed:
        settings = replace(settings, security=security)
        _write_private(target, render_config(settings), 0o640)
        with contextlib.suppress(OSError):
            os.chmod(target.parent, 0o750)
    return settings


def port_is_free(host: str, port: int) -> bool:
    """True when ``host:port`` can be bound right now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe_host = host if host not in {"0.0.0.0", "::"} else ("::" if family == socket.AF_INET6 else "0.0.0.0")
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe_host, port))
        except OSError:
            return False
    return True


def wait_for_free_port(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_is_free(host, port):
            return
        time.sleep(0.2)


def config_permissions_ok(path: str | Path) -> bool:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return False
    return not (mode & (stat.S_IROTH | stat.S_IWOTH))
