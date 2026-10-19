from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import (
    REASON_MESSAGES,
    RejectReason,
    RootRejectedError,
    ValidationContext,
    check_data_dir_overlap,
    validate_root,
)

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")


def reason_for(
    path: str, ctx: ValidationContext, *, allow_system_volume: bool = False
) -> RejectReason | None:
    try:
        validate_root(path, ctx, allow_system_volume=allow_system_volume)
    except RootRejectedError as exc:
        return exc.reason
    return None


def string_ctx(mounts: MountTable, **kwargs: object) -> ValidationContext:
    """Context for path-string and mount rules only (no access to the path)."""
    return ValidationContext(mounts=mounts, check_filesystem=False, **kwargs)  # type: ignore[arg-type]


def test_every_reason_has_a_message() -> None:
    assert set(REASON_MESSAGES) == set(RejectReason)


# --- path string rules -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/home", "/etc", "/usr", "/var", "/boot", "/root", "/mnt", "/media", "/tmp", "/opt"],
)
def test_forbidden_roots(ext4_host: MountTable, path: str) -> None:
    assert reason_for(path, string_ctx(ext4_host)) is RejectReason.FORBIDDEN_PATH


@pytest.mark.parametrize(
    "path",
    ["/proc/1", "/proc/self/root", "/sys/kernel", "/dev/sda", "/dev/shm/x", "/run/user/1000"],
)
def test_inside_pseudo_trees(ext4_host: MountTable, path: str) -> None:
    assert reason_for(path, string_ctx(ext4_host)) is RejectReason.PSEUDO_FILESYSTEM_PATH


def test_removable_media_under_run_media_is_allowed(ext4_host: MountTable) -> None:
    assert reason_for("/run/media/user/USB KEY/photos", string_ctx(ext4_host)) is None


def test_run_media_as_a_whole_is_forbidden(ext4_host: MountTable) -> None:
    # Like /media: it holds every user's removable media.
    assert reason_for("/run/media", string_ctx(ext4_host)) is RejectReason.FORBIDDEN_PATH


@pytest.mark.parametrize("path", ["data", "./data", "mnt/Data", ""])
def test_relative_paths(ext4_host: MountTable, path: str) -> None:
    assert reason_for(path, string_ctx(ext4_host)) is RejectReason.NOT_ABSOLUTE


@pytest.mark.parametrize(
    "path",
    [
        "/mnt/Data/../../etc",
        "/mnt/Data/..",
        "/mnt/./Data",
        "/mnt/Data/",
        "//mnt/Data",
        "/mnt//Data",
    ],
)
def test_non_normalized_paths(ext4_host: MountTable, path: str) -> None:
    assert reason_for(path, string_ctx(ext4_host)) is RejectReason.NOT_NORMALIZED


def test_home_directory_itself(ext4_host: MountTable) -> None:
    ctx = string_ctx(ext4_host, home="/home/user")
    assert reason_for("/home/user", ctx) is RejectReason.HOME_DIRECTORY


def test_home_subfolder_is_on_system_volume(ext4_host: MountTable) -> None:
    ctx = string_ctx(ext4_host, home="/home/user")
    assert reason_for("/home/user/Projects", ctx) is RejectReason.SYSTEM_VOLUME
    assert reason_for("/home/user/Projects", ctx, allow_system_volume=True) is None


# --- mount rules -------------------------------------------------------------------


def test_data_partition_is_accepted(ext4_host: MountTable) -> None:
    assert reason_for("/mnt/Data", string_ctx(ext4_host)) is None
    assert reason_for("/mnt/Data/Backup 2019", string_ctx(ext4_host)) is None


def test_system_volume_is_refused_by_default(ext4_host: MountTable) -> None:
    assert reason_for("/srv/www", string_ctx(ext4_host)) is RejectReason.SYSTEM_VOLUME


def test_btrfs_subvolume_with_other_device_number_is_still_system(btrfs_host: MountTable) -> None:
    # 0:40 differs from the root's 0:35, but the backing device is the same.
    assert reason_for("/srv/data/photos", string_ctx(btrfs_host)) is RejectReason.SYSTEM_VOLUME
    assert reason_for("/home/user/x", string_ctx(btrfs_host)) is RejectReason.SYSTEM_VOLUME


def test_btrfs_on_another_disk_is_accepted(btrfs_host: MountTable) -> None:
    assert reason_for("/mnt/backup/2020", string_ctx(btrfs_host)) is None


def test_virtual_filesystem_outside_pseudo_trees(
    data_disk_table: Callable[..., MountTable],
) -> None:
    mounts = data_disk_table("/mnt/weird", fs_type="proc", source="proc")
    assert reason_for("/mnt/weird", string_ctx(mounts)) is RejectReason.VIRTUAL_FILESYSTEM


def test_unknown_mount_is_refused() -> None:
    from treeaudit.roots.mountinfo import parse_mountinfo

    mounts = parse_mountinfo(b"2 1 8:2 / /mnt rw - ext4 /dev/sda2 rw\n")
    # /srv is not covered by any mount: refuse rather than guess.
    assert reason_for("/srv/x", string_ctx(mounts)) is RejectReason.UNKNOWN_MOUNT


def test_container_requires_read_only_mount(container_mounts: MountTable) -> None:
    ctx = string_ctx(container_mounts, require_read_only_mount=True)
    assert reason_for("/audit/roots/main", ctx) is None
    assert reason_for("/audit/roots/writable", ctx) is RejectReason.NOT_READ_ONLY


# --- filesystem checks (real temporary directories) ----------------------------------


@pytest.fixture
def fs_ctx(tmp_path: Path, data_disk_table: Callable[..., MountTable]) -> ValidationContext:
    """Pretend tmp_path is a separate data disk so only filesystem rules apply."""
    return ValidationContext(mounts=data_disk_table(str(tmp_path)), check_filesystem=True)


def test_existing_directory_is_accepted(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    target = tmp_path / "audit me"
    target.mkdir()
    assert reason_for(str(target), fs_ctx) is None


def test_missing_path(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    assert reason_for(str(tmp_path / "missing"), fs_ctx) is RejectReason.NOT_FOUND


def test_regular_file(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    assert reason_for(str(target), fs_ctx) is RejectReason.NOT_A_DIRECTORY


def test_path_below_a_file(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    (tmp_path / "file.txt").write_text("x")
    assert reason_for(str(tmp_path / "file.txt" / "sub"), fs_ctx) is RejectReason.NOT_FOUND


def test_symlink_to_directory(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert reason_for(str(link), fs_ctx) is RejectReason.SYMLINK


def test_symlink_pointing_outside(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    link = tmp_path / "etc-link"
    link.symlink_to("/etc")
    assert reason_for(str(link), fs_ctx) is RejectReason.SYMLINK


def test_directory_reached_through_symlinked_parent(
    tmp_path: Path, fs_ctx: ValidationContext
) -> None:
    (tmp_path / "real" / "child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    assert reason_for(str(tmp_path / "alias" / "child"), fs_ctx) is RejectReason.SYMLINK


def test_broken_symlink(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    link = tmp_path / "broken"
    link.symlink_to(tmp_path / "nowhere")
    assert reason_for(str(link), fs_ctx) is RejectReason.SYMLINK


@non_root_only
def test_unreachable_directory(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    locked = tmp_path / "locked"
    (locked / "inside").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        assert reason_for(str(locked / "inside"), fs_ctx) is RejectReason.PERMISSION_DENIED
    finally:
        locked.chmod(0o700)


# --- data directory ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("root", "data_dir"),
    [
        ("/mnt/Data/a", "/mnt/Data/a"),
        ("/mnt/Data/a", "/mnt/Data/a/treeaudit"),
        ("/mnt/Data/a/b", "/mnt/Data/a"),
    ],
)
def test_data_dir_overlap(root: str, data_dir: str) -> None:
    with pytest.raises(RootRejectedError) as info:
        check_data_dir_overlap(root, data_dir, resolve=False)
    assert info.value.reason is RejectReason.DATA_DIR_OVERLAP


def test_sibling_with_common_prefix_does_not_overlap() -> None:
    check_data_dir_overlap("/mnt/Data/a", "/mnt/Data/ab", resolve=False)


def test_data_dir_overlap_through_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "data-link").symlink_to(root)
    with pytest.raises(RootRejectedError):
        check_data_dir_overlap(str(root), str(tmp_path / "data-link" / "treeaudit"))


def test_validate_root_checks_data_dir(tmp_path: Path, fs_ctx: ValidationContext) -> None:
    target = tmp_path / "root"
    target.mkdir()
    ctx = ValidationContext(mounts=fs_ctx.mounts, data_dir=str(target / "data"))
    assert reason_for(str(target), ctx) is RejectReason.DATA_DIR_OVERLAP
