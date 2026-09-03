from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_MAX_OWNER_PASSWORD_BYTES = 4096
_MAX_OWNER_PASSWORD_LENGTH = 1024
_DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_KNOWN_PASSWORD_PLACEHOLDERS = frozenset(
    {
        "<set-a-local-owner-password>",
        "change-me",
        "changeme",
        "example",
        "password",
        "replace-me",
        "your-password-here",
    }
)


class SettingsError(ValueError):
    """Raised when process configuration cannot be used safely."""


@dataclass(frozen=True)
class Settings:
    environment: str
    data_directory: Path
    owner_password: str
    bind_host: str
    port: int
    workers: int
    allowed_hosts: frozenset[str]
    webhook_url: str | None
    webhook_enabled: bool
    static_root: Path | None
    release_mode: bool
    delivery_poll_seconds: float


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if environ is None else environ
    raw_data_directory = values.get("MARKET_MONITOR_DATA_DIR", "").strip()
    if not raw_data_directory:
        raise SettingsError("MARKET_MONITOR_DATA_DIR is required")

    environment = values.get("MARKET_MONITOR_ENV", "development").strip().lower()
    if not environment:
        environment = "development"
    if environment not in {"development", "release", "production"}:
        raise SettingsError("MARKET_MONITOR_ENV is not supported")
    release_mode = environment in {"release", "production"}
    owner_password = _load_owner_password(values)
    bind_host = _loopback_host(values.get("MARKET_MONITOR_BIND_HOST", "127.0.0.1"))
    port = _bounded_integer(values.get("MARKET_MONITOR_PORT", "8000"), "port", 1, 65535)
    workers = _bounded_integer(values.get("MARKET_MONITOR_WORKERS", "1"), "workers", 1, 1)
    allowed_hosts = _allowed_hosts(values.get("MARKET_MONITOR_ALLOWED_HOSTS", ""))
    webhook_url = values.get("MARKET_MONITOR_WEBHOOK_URL", "").strip() or None
    webhook_enabled = _boolean(
        values.get("MARKET_MONITOR_WEBHOOK_ENABLED", "false"), "webhook enabled"
    )
    if webhook_enabled and webhook_url is None:
        raise SettingsError("webhook cannot be enabled without an endpoint")
    static_root = _static_root(values.get("MARKET_MONITOR_STATIC_ROOT", ""), release_mode)
    delivery_poll_seconds = _bounded_float(
        values.get("MARKET_MONITOR_DELIVERY_POLL_SECONDS", "1"),
        "delivery poll interval",
        0.05,
        60.0,
    )
    return Settings(
        environment=environment,
        data_directory=Path(raw_data_directory),
        owner_password=owner_password,
        bind_host=bind_host,
        port=port,
        workers=workers,
        allowed_hosts=allowed_hosts,
        webhook_url=webhook_url,
        webhook_enabled=webhook_enabled,
        static_root=static_root,
        release_mode=release_mode,
        delivery_poll_seconds=delivery_poll_seconds,
    )


def _load_owner_password(values: Mapping[str, str]) -> str:
    direct = values.get("MARKET_MONITOR_OWNER_PASSWORD", "")
    file_name = values.get("MARKET_MONITOR_OWNER_PASSWORD_FILE", "").strip()
    if direct.strip() and file_name:
        raise SettingsError("owner password must use exactly one configured source")
    if direct.strip():
        return _validate_owner_password(direct)
    if not file_name:
        raise SettingsError("MARKET_MONITOR_OWNER_PASSWORD is required")
    try:
        path = Path(file_name)
        size = path.stat().st_size
        if not path.is_file() or size <= 0 or size > _MAX_OWNER_PASSWORD_BYTES:
            raise OSError("secret file is outside the accepted bounds")
        content = path.read_bytes()
    except (OSError, ValueError):
        raise SettingsError("owner password file is unavailable") from None
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsError("owner password file is invalid") from None
    if decoded.endswith("\r\n"):
        decoded = decoded[:-2]
    elif decoded.endswith("\n"):
        decoded = decoded[:-1]
    if "\x00" in decoded:
        raise SettingsError("owner password file is invalid")
    return _validate_owner_password(decoded)


def _validate_owner_password(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(value) > _MAX_OWNER_PASSWORD_LENGTH
        or normalized in _KNOWN_PASSWORD_PLACEHOLDERS
        or (normalized.startswith("<") and normalized.endswith(">"))
    ):
        raise SettingsError("owner password is invalid")
    return value


def _loopback_host(value: str) -> str:
    host = value.strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise SettingsError("bind host must be a loopback address")
    if host == "localhost":
        return host
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError:
        raise SettingsError("bind host must be a loopback address") from None
    return host


def _allowed_hosts(value: str) -> frozenset[str]:
    if not value.strip():
        return _DEFAULT_ALLOWED_HOSTS
    hosts = frozenset(_host_header_name(item) for item in value.split(",") if item.strip())
    if not hosts:
        raise SettingsError("allowed hosts must not be empty")
    for host in hosts:
        loopback = "::1" if host == "[::1]" else host
        if loopback not in _LOOPBACK_HOSTS:
            raise SettingsError("allowed hosts must be loopback-only")
    return hosts


def _host_header_name(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if host == "::1":
        return "[::1]"
    if host in {"localhost", "127.0.0.1", "[::1]"}:
        return host
    raise SettingsError("allowed hosts must be loopback-only")


def _bounded_integer(value: str, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value.strip())
    except ValueError:
        raise SettingsError(f"{name} is invalid") from None
    if parsed < minimum or parsed > maximum:
        raise SettingsError(f"{name} is outside the accepted bounds")
    return parsed


def _bounded_float(value: str, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value.strip())
    except ValueError:
        raise SettingsError(f"{name} is invalid") from None
    if not minimum <= parsed <= maximum:
        raise SettingsError(f"{name} is outside the accepted bounds")
    return parsed


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise SettingsError(f"{name} is invalid")


def _static_root(value: str, release_mode: bool) -> Path | None:
    root = (
        Path(value).expanduser().resolve()
        if value.strip()
        else Path(__file__).resolve().parents[4] / "apps" / "web" / "dist"
    )
    complete = root.is_dir() and (root / "index.html").is_file() and (root / "assets").is_dir()
    if complete:
        return root
    if release_mode or value.strip():
        raise SettingsError("static assets are unavailable")
    return None
