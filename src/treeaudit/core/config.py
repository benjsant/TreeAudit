"""Loading and validation of the TreeAudit configuration file (TOML).

This module only checks the *shape* of the configuration. Whether a root path
is acceptable for auditing is decided by ``treeaudit.roots.validation``.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from treeaudit.core.errors import ConfigError

CONFIG_ENV_VAR = "TREEAUDIT_CONFIG"
CONTAINER_ENV_VAR = "TREEAUDIT_CONTAINER"

DEFAULT_PORT = 8765
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
# Inside a container the server must listen on all interfaces of the container's
# own network namespace; exposure is restricted by the host-side port publishing
# (docs/DECISIONS.md D-010).
CONTAINER_BIND_HOST = "0.0.0.0"  # noqa: S104

ROOT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")

_TOP_LEVEL_KEYS = frozenset({"data_dir", "bind_host", "port", "roots"})
_ROOT_KEYS = frozenset(
    {"id", "path", "label", "allow_system_volume", "cross_mounts", "display_path"}
)
# In Docker, the host path of a root is only known from the environment
# (compose passes it); it is used for display only, never to access files.
DISPLAY_PATH_ENV_PREFIX = "TREEAUDIT_DISPLAY_PATH_"


@dataclass(frozen=True, slots=True)
class RootConfig:
    """An audit root declared by the user."""

    id: str
    path: str
    label: str
    allow_system_volume: bool = False
    cross_mounts: bool = False
    display_path: str | None = None
    """Path shown to the user (the host path in Docker). Display only."""

    @property
    def shown_path(self) -> str:
        return self.display_path or self.path


@dataclass(frozen=True, slots=True)
class AppConfig:
    """The whole application configuration."""

    data_dir: str
    roots: tuple[RootConfig, ...]
    bind_host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    container_mode: bool = False

    def get_root(self, root_id: str) -> RootConfig | None:
        """Return the root with this identifier, or None."""
        for root in self.roots:
            if root.id == root_id:
                return root
        return None


def is_container_mode(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when running inside the TreeAudit Docker image."""
    env = os.environ if environ is None else environ
    return env.get(CONTAINER_ENV_VAR) == "1"


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the configuration path: $TREEAUDIT_CONFIG, else the XDG location."""
    env = os.environ if environ is None else environ
    explicit = env.get(CONFIG_ENV_VAR)
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "treeaudit" / "config.toml"


def display_path_env_var(root_id: str) -> str:
    return DISPLAY_PATH_ENV_PREFIX + root_id.upper().replace("-", "_")


def apply_display_paths(config: AppConfig, environ: Mapping[str, str]) -> AppConfig:
    """Override display paths from the environment (set by compose in Docker)."""
    roots = []
    for root in config.roots:
        value = environ.get(display_path_env_var(root.id))
        if value:
            root = dataclasses.replace(
                root, display_path=_require_absolute(value, display_path_env_var(root.id))
            )
        roots.append(root)
    return dataclasses.replace(config, roots=tuple(roots))


def load_config(
    path: Path, *, container_mode: bool = False, environ: Mapping[str, str] | None = None
) -> AppConfig:
    """Read and validate the configuration file at ``path``."""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"configuration file is not readable: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    config = parse_config(raw, container_mode=container_mode)
    return apply_display_paths(config, os.environ if environ is None else environ)


def parse_config(raw: Mapping[str, Any], *, container_mode: bool = False) -> AppConfig:
    """Validate an already-decoded configuration mapping."""
    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, "top level")

    data_dir = _require_path_string(raw.get("data_dir"), "data_dir")

    default_host = CONTAINER_BIND_HOST if container_mode else "127.0.0.1"
    bind_host = raw.get("bind_host", default_host)
    if not isinstance(bind_host, str):
        raise ConfigError("bind_host must be a string")
    check_bind_host(bind_host, container_mode=container_mode)

    port = raw.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ConfigError("port must be an integer between 1024 and 65535")

    raw_roots = raw.get("roots", [])
    if not isinstance(raw_roots, list):
        raise ConfigError("roots must be an array of tables ([[roots]])")
    roots = tuple(_parse_root(item, index) for index, item in enumerate(raw_roots))

    seen: set[str] = set()
    for root in roots:
        if root.id in seen:
            raise ConfigError(f"duplicate root id: {root.id!r}")
        seen.add(root.id)

    return AppConfig(
        data_dir=data_dir,
        roots=roots,
        bind_host=bind_host,
        port=port,
        container_mode=container_mode,
    )


def check_bind_host(bind_host: str, *, container_mode: bool) -> None:
    """Refuse any listening address reachable from outside this machine."""
    if bind_host in LOOPBACK_HOSTS:
        return
    if container_mode and bind_host == CONTAINER_BIND_HOST:
        return
    raise ConfigError(
        f"bind_host {bind_host!r} is not allowed: the server must only be reachable "
        "from this machine (see docs/DECISIONS.md D-010)"
    )


def _parse_root(item: Any, index: int) -> RootConfig:
    where = f"roots[{index}]"
    if not isinstance(item, dict):
        raise ConfigError(f"{where} must be a table")
    _reject_unknown_keys(item, _ROOT_KEYS, where)

    root_id = item.get("id")
    if not isinstance(root_id, str) or not ROOT_ID_PATTERN.fullmatch(root_id):
        raise ConfigError(
            f"{where}.id must match {ROOT_ID_PATTERN.pattern} "
            "(lowercase letters, digits, '-' and '_')"
        )

    path = _require_path_string(item.get("path"), f"{where}.path")

    label = item.get("label", root_id)
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise ConfigError(f"{where}.label must be a non-empty string of at most 200 characters")

    flags: dict[str, bool] = {}
    for key in ("allow_system_volume", "cross_mounts"):
        value = item.get(key, False)
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.{key} must be true or false")
        flags[key] = value

    display_path = item.get("display_path")
    if display_path is not None:
        display_path = _require_absolute(display_path, f"{where}.display_path")

    return RootConfig(id=root_id, path=path, label=label, display_path=display_path, **flags)


def _require_path_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ConfigError(f"{name} must not contain a NUL character")
    return value


def _require_absolute(value: Any, name: str) -> str:
    text = _require_path_string(value, name)
    if not text.startswith("/"):
        raise ConfigError(f"{name} must be an absolute path")
    return text


def _reject_unknown_keys(table: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) at {where}: {', '.join(unknown)}")
