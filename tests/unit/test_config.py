from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from treeaudit.core.config import (
    default_config_path,
    is_container_mode,
    load_config,
    parse_config,
)
from treeaudit.core.errors import ConfigError


def minimal(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "data_dir": "/srv/treeaudit",
        "roots": [{"id": "backup", "path": "/mnt/Data/Backup"}],
    }
    raw.update(overrides)
    return raw


def test_minimal_config_defaults() -> None:
    config = parse_config(minimal())
    assert config.bind_host == "127.0.0.1"
    assert config.port == 8765
    assert not config.container_mode
    root = config.roots[0]
    assert (root.id, root.path, root.label) == ("backup", "/mnt/Data/Backup", "backup")
    assert not root.allow_system_volume
    assert not root.cross_mounts
    assert config.get_root("backup") is root
    assert config.get_root("other") is None


def test_no_roots_is_valid() -> None:
    assert parse_config({"data_dir": "/srv/treeaudit"}).roots == ()


def test_container_mode_defaults_to_all_interfaces_inside_container() -> None:
    assert parse_config(minimal(), container_mode=True).bind_host == "0.0.0.0"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.org"])
def test_non_loopback_bind_refused_natively(host: str) -> None:
    with pytest.raises(ConfigError, match="bind_host"):
        parse_config(minimal(bind_host=host))


def test_only_0000_allowed_in_container() -> None:
    parse_config(minimal(bind_host="0.0.0.0"), container_mode=True)
    with pytest.raises(ConfigError):
        parse_config(minimal(bind_host="192.168.1.10"), container_mode=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_bind_accepted(host: str) -> None:
    assert parse_config(minimal(bind_host=host)).bind_host == host


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, "8765", True, 8765.0])
def test_invalid_port(port: object) -> None:
    with pytest.raises(ConfigError, match="port"):
        parse_config(minimal(port=port))


@pytest.mark.parametrize(
    "root_id", ["", "Backup", "../etc", "/etc", "a b", "a/b", "-leading", "x" * 64, 3]
)
def test_invalid_root_id(root_id: object) -> None:
    with pytest.raises(ConfigError, match="id"):
        parse_config(minimal(roots=[{"id": root_id, "path": "/mnt/x"}]))


def test_duplicate_root_ids() -> None:
    roots = [{"id": "a", "path": "/mnt/x"}, {"id": "a", "path": "/mnt/y"}]
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(minimal(roots=roots))


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        parse_config(minimal(data_dirs="/x"))
    with pytest.raises(ConfigError, match="unknown"):
        parse_config(minimal(roots=[{"id": "a", "path": "/mnt/x", "allow_system_volumes": True}]))


@pytest.mark.parametrize("flag", ["allow_system_volume", "cross_mounts"])
def test_flags_must_be_booleans(flag: str) -> None:
    with pytest.raises(ConfigError, match=flag):
        parse_config(minimal(roots=[{"id": "a", "path": "/mnt/x", flag: "yes"}]))


@pytest.mark.parametrize("path", ["", None, 3, "/mnt/\x00x"])
def test_invalid_root_path(path: object) -> None:
    with pytest.raises(ConfigError, match="path"):
        parse_config(minimal(roots=[{"id": "a", "path": path}]))


def test_missing_data_dir() -> None:
    with pytest.raises(ConfigError, match="data_dir"):
        parse_config({"roots": []})


def test_load_config_from_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'data_dir = "/srv/treeaudit"\n'
        "[[roots]]\n"
        'id = "photos"\n'
        'label = "Photos de famille"\n'
        'path = "/mnt/Data/Photos"\n'
        "allow_system_volume = true\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.roots[0].label == "Photos de famille"
    assert config.roots[0].allow_system_volume


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")


def test_load_config_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("data_dir = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_default_config_path() -> None:
    assert default_config_path({"TREEAUDIT_CONFIG": "/x/c.toml"}) == Path("/x/c.toml")
    assert default_config_path({"XDG_CONFIG_HOME": "/cfg"}) == Path("/cfg/treeaudit/config.toml")


def test_container_mode_detection() -> None:
    assert is_container_mode({"TREEAUDIT_CONTAINER": "1"})
    assert not is_container_mode({"TREEAUDIT_CONTAINER": "yes"})
    assert not is_container_mode({})
