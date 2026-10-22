from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import prepare_data_dir
from treeaudit.core.errors import ConfigError
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.registry import check_data_dir_separation, evaluate_roots
from treeaudit.roots.validation import RejectReason


def config_for(data_dir: Path, *roots: RootConfig, container_mode: bool = False) -> AppConfig:
    return AppConfig(data_dir=str(data_dir), roots=roots, container_mode=container_mode)


def test_evaluate_roots_keeps_rejected_roots_with_reason(
    tmp_path: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    good = tmp_path / "good"
    good.mkdir()
    config = config_for(
        tmp_path / "data",
        RootConfig(id="good", path=str(good), label="Good"),
        RootConfig(id="missing", path=str(tmp_path / "missing"), label="Missing"),
    )
    statuses = evaluate_roots(config, data_disk_table(str(tmp_path)), home=None)
    assert [(s.root.id, s.accepted, s.reason) for s in statuses] == [
        ("good", True, None),
        ("missing", False, RejectReason.NOT_FOUND),
    ]


def test_container_mode_requires_read_only_mounts(
    tmp_path: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    config = config_for(
        tmp_path / "data", RootConfig(id="r", path=str(root), label="r"), container_mode=True
    )
    rw = evaluate_roots(config, data_disk_table(str(root), options="rw"), home=None)
    ro = evaluate_roots(config, data_disk_table(str(root), options="ro"), home=None)
    assert rw[0].reason is RejectReason.NOT_READ_ONLY
    assert ro[0].accepted
    # A root must be its own bind mount: otherwise it is the container's own files.
    parent = evaluate_roots(config, data_disk_table(str(tmp_path), options="ro"), home=None)
    assert parent[0].reason is RejectReason.NOT_MOUNTED


def test_data_dir_inside_a_root_prevents_startup(tmp_path: Path) -> None:
    root = tmp_path / "root"
    config = config_for(root / "treeaudit-data", RootConfig(id="r", path=str(root), label="r"))
    with pytest.raises(ConfigError, match="overlaps"):
        check_data_dir_separation(config)


def test_root_inside_data_dir_prevents_startup(tmp_path: Path) -> None:
    config = config_for(tmp_path, RootConfig(id="r", path=str(tmp_path / "sub"), label="r"))
    with pytest.raises(ConfigError, match="overlaps"):
        check_data_dir_separation(config)


def test_prepare_data_dir_creates_private_directory(tmp_path: Path) -> None:
    target = tmp_path / "a" / "data"
    prepare_data_dir(str(target))
    assert target.is_dir()
    assert stat.S_IMODE(os.stat(target).st_mode) & 0o077 == 0


def test_prepare_data_dir_warns_when_shared(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "data"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    with caplog.at_level(logging.WARNING):
        prepare_data_dir(str(target))
    assert "accessible to other users" in caplog.text


def test_prepare_data_dir_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(ConfigError, match="real directory"):
        prepare_data_dir(str(tmp_path / "link"))


def test_prepare_data_dir_refuses_relative_path() -> None:
    with pytest.raises(ConfigError, match="absolute"):
        prepare_data_dir("data")


# --- personal folder (D-037) -------------------------------------------------------------


def test_personal_folder_in_the_container_comes_from_the_launcher() -> None:
    from treeaudit.roots.personal import CONTAINER_HOME_DIR, HOME_PATH_ENV_VAR, PRIVATE_PATHS
    from treeaudit.roots.registry import with_personal_folder

    config = AppConfig(data_dir="/data", roots=(), container_mode=True)
    assert with_personal_folder(config, {}, None) == config
    assert with_personal_folder(config, {HOME_PATH_ENV_VAR: "relative"}, None) == config
    added = with_personal_folder(config, {HOME_PATH_ENV_VAR: "/home/me"}, "/ignored")
    (root,) = added.roots
    assert (root.id, root.path, root.display_path) == ("home", CONTAINER_HOME_DIR, "/home/me")
    assert root.personal and root.detected and root.allow_system_volume
    assert root.private_paths == PRIVATE_PATHS and b".ssh" in PRIVATE_PATHS
    assert added.declared_roots == ()


def test_personal_folder_natively_only_when_enabled() -> None:
    from treeaudit.roots.registry import with_personal_folder

    config = AppConfig(data_dir="/srv/data", roots=())
    assert with_personal_folder(config, {}, "/home/me") == config
    enabled = AppConfig(data_dir="/srv/data", roots=(), personal_home=True)
    assert [r.path for r in with_personal_folder(enabled, {}, "/home/me").roots] == ["/home/me"]
    assert with_personal_folder(enabled, {}, None) == enabled


def test_own_dirs_are_identities_of_existing_folders(tmp_path: Path) -> None:
    from treeaudit.core.datadir import own_dirs

    data = tmp_path / "data"
    data.mkdir()
    st = os.stat(data)
    assert own_dirs(str(data), None, str(tmp_path / "missing")) == {(st.st_dev, st.st_ino)}
