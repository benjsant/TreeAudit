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


def test_display_path_from_config_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'data_dir = "/srv/t"\n'
        '[[roots]]\nid = "main"\npath = "/audit/roots/main"\n'
        '[[roots]]\nid = "my-disk"\npath = "/mnt/x"\ndisplay_path = "/media/x"\n',
        encoding="utf-8",
    )
    config = load_config(path, environ={"TREEAUDIT_DISPLAY_PATH_MAIN": "/mnt/Data/Backup"})
    main_root, other = config.roots
    assert main_root.shown_path == "/mnt/Data/Backup"
    assert main_root.path == "/audit/roots/main"  # access path unchanged
    assert other.shown_path == "/media/x"
    overridden = load_config(path, environ={"TREEAUDIT_DISPLAY_PATH_MY_DISK": "/elsewhere"})
    assert overridden.roots[1].shown_path == "/elsewhere"


def test_display_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="absolute"):
        parse_config(minimal(roots=[{"id": "a", "path": "/x", "display_path": "rel"}]))
    path = tmp_path / "config.toml"
    path.write_text('data_dir = "/srv/t"\n[[roots]]\nid = "main"\npath = "/x"\n', "utf-8")
    with pytest.raises(ConfigError, match="absolute"):
        load_config(path, environ={"TREEAUDIT_DISPLAY_PATH_MAIN": "relative"})


def test_archive_dir_is_optional_and_absolute() -> None:
    assert parse_config(minimal()).archive_dir is None
    config = parse_config(minimal(archive_dir="/mnt/Archives"))
    assert config.archive_dir == "/mnt/Archives"
    assert config.shown_archive_dir == "/mnt/Archives"
    for bad in ("relative/dir", "", 3, "/mnt/\x00x"):
        with pytest.raises(ConfigError, match="archive_dir"):
            parse_config(minimal(archive_dir=bad))


def test_archive_display_path_comes_from_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('data_dir = "/data"\narchive_dir = "/archives"\n', encoding="utf-8")
    env = {"TREEAUDIT_ARCHIVE_DISPLAY_PATH": "/run/media/me/USB/archives"}
    config = load_config(path, container_mode=True, environ=env)
    assert config.archive_dir == "/archives"
    assert config.shown_archive_dir == "/run/media/me/USB/archives"
    with pytest.raises(ConfigError, match="TREEAUDIT_ARCHIVE_DISPLAY_PATH"):
        load_config(path, environ={"TREEAUDIT_ARCHIVE_DISPLAY_PATH": "relative"})
    # Without an archive directory, a display path is meaningless and ignored.
    path.write_text('data_dir = "/data"\n', encoding="utf-8")
    assert load_config(path, environ=env).shown_archive_dir is None


@pytest.mark.parametrize("root_id", ["home", "ext-usb-123456"])
def test_ids_reserved_for_detected_roots(root_id: str) -> None:
    with pytest.raises(ConfigError, match="reserved"):
        parse_config(minimal(roots=[{"id": root_id, "path": "/mnt/x"}]))


def test_personal_home_flag() -> None:
    assert parse_config(minimal()).personal_home is False
    assert parse_config(minimal(personal_home=True)).personal_home is True
    with pytest.raises(ConfigError, match="personal_home"):
        parse_config(minimal(personal_home="yes"))


def test_native_defaults_need_no_file(tmp_path: Path) -> None:
    from treeaudit.core.config import native_defaults

    config = native_defaults({"HOME": str(tmp_path)})
    base = tmp_path.resolve() / ".local" / "share" / "treeaudit"
    assert config.data_dir == str(base / "index")
    assert config.archive_dir == str(base / "archives")
    assert config.external_disks and config.personal_home
    assert config.bind_host == "127.0.0.1" and config.roots == ()
    xdg = native_defaults({"HOME": "/nowhere", "XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert xdg.data_dir == str(tmp_path.resolve() / "xdg" / "treeaudit" / "index")
    relative = native_defaults({"HOME": str(tmp_path), "XDG_DATA_HOME": "relative"})
    assert relative.data_dir == str(base / "index")
