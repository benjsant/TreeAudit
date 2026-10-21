"""Validation of the archive directory (docs/DECISIONS.md D-032)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from treeaudit.archive import destination
from treeaudit.archive.destination import (
    ARCHIVE_DIR_MESSAGES,
    ArchiveDirContext,
    ArchiveDirReason,
    ArchiveDirRejectedError,
    evaluate_archive_dir,
    validate_archive_dir,
)
from treeaudit.core.config import parse_config
from treeaudit.roots.mountinfo import MountEntry, MountTable, parse_mountinfo

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")

# Inside the TreeAudit container: audited folder /Backup of 259:4, data dir on 0:62.
CONTAINER = b"""\
500 400 0:60 / / rw,relatime - overlay overlay rw,lowerdir=/l,upperdir=/u,workdir=/w
501 500 259:4 /Backup /audit/roots/main ro,relatime - ext4 /dev/nvme0n1p4 rw
503 500 0:61 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
504 500 0:62 / /data rw,relatime - ext4 /dev/nvme0n1p4 rw
"""


def make_entry(
    mount_point: str,
    *,
    major_minor: str,
    fs_type: str = "ext4",
    source: str = "/dev/sdz1",
    options: str = "rw,relatime",
    mount_id: int = 1,
) -> MountEntry:
    return MountEntry(
        mount_id=mount_id,
        parent_id=0,
        major_minor=major_minor,
        fs_root="/",
        mount_point=mount_point,
        mount_options=frozenset(options.split(",")),
        fs_type=fs_type,
        source=source,
        super_options=frozenset({"rw"}),
    )


def table(*extra: tuple[str, str, str]) -> MountTable:
    """System on 259:3; each extra item is (mount point, fs type, options) on 8:1."""
    entries = [make_entry("/", major_minor="259:3", source="/dev/nvme0n1p3", mount_id=1)]
    for index, (point, fs_type, options) in enumerate(extra, start=2):
        entries.append(
            make_entry(point, major_minor="8:1", fs_type=fs_type, options=options, mount_id=index)
        )
    return MountTable(entries=tuple(entries))


def reason_for(path: str, ctx: ArchiveDirContext) -> ArchiveDirReason | None:
    try:
        validate_archive_dir(path, ctx)
    except ArchiveDirRejectedError as exc:
        return exc.reason
    return None


def string_ctx(mounts: MountTable | None = None, **kwargs: object) -> ArchiveDirContext:
    """Context without access to the path (string and mount rules only)."""
    return ArchiveDirContext(  # type: ignore[arg-type]
        mounts=mounts or table(), check_filesystem=False, **kwargs
    )


@pytest.fixture
def tmp_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's tmp_path lives below /tmp, refused for archives: lift that one rule."""
    trees = tuple(tree for tree in destination.SYSTEM_TREES if tree != "/tmp")
    monkeypatch.setattr(destination, "SYSTEM_TREES", trees)


def test_every_reason_has_a_message() -> None:
    assert set(ARCHIVE_DIR_MESSAGES) == set(ArchiveDirReason)


def test_ordinary_folder_on_a_data_disk_is_accepted() -> None:
    mounts = table(("/mnt/Archives", "ext4", "rw,relatime"))
    assert reason_for("/mnt/Archives/treeaudit", string_ctx(mounts)) is None


def test_system_volume_is_allowed_for_archives() -> None:
    # Unlike roots, archives may go to the system disk (e.g. ~/Archives).
    ctx = string_ctx(home="/home/me")
    assert reason_for("/home/me/Archives", ctx) is None


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("relative/dir", ArchiveDirReason.NOT_ABSOLUTE),
        ("/mnt/x/../y", ArchiveDirReason.NOT_NORMALIZED),
        ("/mnt/x/", ArchiveDirReason.NOT_NORMALIZED),
        ("//mnt/x", ArchiveDirReason.NOT_NORMALIZED),
        ("/", ArchiveDirReason.SYSTEM_PATH),
        ("/home", ArchiveDirReason.SYSTEM_PATH),
        ("/mnt", ArchiveDirReason.SYSTEM_PATH),
        ("/media", ArchiveDirReason.SYSTEM_PATH),
        ("/etc/treeaudit", ArchiveDirReason.SYSTEM_PATH),
        ("/usr/local/archives", ArchiveDirReason.SYSTEM_PATH),
        ("/var/tmp/archives", ArchiveDirReason.SYSTEM_PATH),
        ("/tmp/archives", ArchiveDirReason.SYSTEM_PATH),
        ("/proc/self", ArchiveDirReason.SYSTEM_PATH),
        ("/run/user/1000", ArchiveDirReason.SYSTEM_PATH),
        ("/root/archives", ArchiveDirReason.SYSTEM_PATH),
    ],
)
def test_path_string_rules(path: str, reason: ArchiveDirReason) -> None:
    assert reason_for(path, string_ctx()) is reason


def test_removable_media_below_run_media_is_allowed() -> None:
    mounts = table(("/run/media/me/USB", "exfat", "rw,nosuid"))
    assert reason_for("/run/media/me/USB/archives", string_ctx(mounts)) is None


def test_whole_home_is_refused() -> None:
    assert reason_for("/home/me", string_ctx(home="/home/me")) is ArchiveDirReason.HOME_DIRECTORY


@pytest.mark.parametrize(
    ("fs_type", "options", "reason"),
    [
        ("tmpfs", "rw", ArchiveDirReason.VOLATILE_FILESYSTEM),
        ("ramfs", "rw", ArchiveDirReason.VOLATILE_FILESYSTEM),
        ("sysfs", "rw", ArchiveDirReason.VIRTUAL_FILESYSTEM),
        ("ext4", "ro,relatime", ArchiveDirReason.READ_ONLY_MOUNT),
    ],
)
def test_mount_rules(fs_type: str, options: str, reason: ArchiveDirReason) -> None:
    mounts = table(("/mnt/Archives", fs_type, options))
    assert reason_for("/mnt/Archives/x", string_ctx(mounts)) is reason


def test_unknown_mount() -> None:
    mounts = MountTable(entries=(make_entry("/mnt/other", major_minor="8:1"),))
    assert reason_for("/mnt/Archives", string_ctx(mounts)) is ArchiveDirReason.UNKNOWN_MOUNT


@pytest.mark.parametrize(
    ("root", "path"),
    [
        ("/mnt/Data/Backup", "/mnt/Data/Backup"),
        ("/mnt/Data/Backup", "/mnt/Data/Backup/archives"),
        ("/mnt/Data/Backup/sub", "/mnt/Data/Backup"),
        ("/mnt/Data", "/mnt/Data/Backup/archives"),
    ],
)
def test_overlap_with_a_root_is_refused(root: str, path: str) -> None:
    ctx = string_ctx(root_paths=("/mnt/Other", root))
    assert reason_for(path, ctx) is ArchiveDirReason.ROOT_OVERLAP


def test_sibling_of_a_root_is_accepted() -> None:
    ctx = string_ctx(root_paths=("/mnt/Data/Backup",), data_dir="/srv/treeaudit")
    assert reason_for("/mnt/Data/BackupArchives", ctx) is None


def test_data_dir_inside_archive_dir_is_refused() -> None:
    ctx = string_ctx(data_dir="/mnt/Archives/treeaudit-data")
    assert reason_for("/mnt/Archives", ctx) is ArchiveDirReason.DATA_DIR_OVERLAP


def test_archive_dir_inside_data_dir_is_refused() -> None:
    ctx = string_ctx(data_dir="/mnt/Archives")
    assert reason_for("/mnt/Archives/out", ctx) is ArchiveDirReason.DATA_DIR_OVERLAP


# --- In the container --------------------------------------------------------

CONTAINER_WITH_ARCHIVES = CONTAINER + b"505 500 8:1 /Archives /archives rw - ext4 /dev/sda1 rw\n"


def container_ctx(content: bytes, **kwargs: object) -> ArchiveDirContext:
    return ArchiveDirContext(  # type: ignore[arg-type]
        mounts=parse_mountinfo(content), check_filesystem=False, container_mode=True, **kwargs
    )


def test_container_requires_a_dedicated_mount() -> None:
    # Without the bind mount, /archives would be inside the container itself.
    assert reason_for("/archives", container_ctx(CONTAINER)) is ArchiveDirReason.NOT_MOUNTED
    assert reason_for("/archives", container_ctx(CONTAINER_WITH_ARCHIVES)) is None


def test_container_subfolder_of_the_mount_is_refused() -> None:
    content = CONTAINER + b"505 500 8:1 /Archives /archives rw - ext4 /dev/sda1 rw\n"
    assert reason_for("/archives/sub", container_ctx(content)) is ArchiveDirReason.NOT_MOUNTED


def test_container_read_only_archive_mount_is_refused() -> None:
    content = CONTAINER + b"505 500 8:1 /Archives /archives ro - ext4 /dev/sda1 rw\n"
    assert reason_for("/archives", container_ctx(content)) is ArchiveDirReason.READ_ONLY_MOUNT


@pytest.mark.parametrize("host_folder", [b"/Backup", b"/Backup/archives", b"/"])
def test_container_detects_a_root_and_archives_on_the_same_host_tree(host_folder: bytes) -> None:
    # /audit/roots/main exposes /Backup of device 259:4 (see CONTAINER).
    line = b"505 500 259:4 " + host_folder + b" /archives rw - ext4 /dev/nvme0n1p4 rw\n"
    ctx = container_ctx(CONTAINER + line, root_paths=("/audit/roots/main",))
    assert reason_for("/archives", ctx) is ArchiveDirReason.ROOT_OVERLAP


def test_container_same_device_but_separate_folders_is_accepted() -> None:
    line = b"505 500 259:4 /Archives /archives rw - ext4 /dev/nvme0n1p4 rw\n"
    ctx = container_ctx(CONTAINER + line, root_paths=("/audit/roots/main",))
    assert reason_for("/archives", ctx) is None


def test_container_detects_archives_on_the_data_dir_host_tree() -> None:
    # /data exposes the root of device 0:62 (see CONTAINER).
    line = b"505 500 0:62 /archives /archives rw - ext4 /dev/nvme0n1p4 rw\n"
    ctx = container_ctx(CONTAINER + line, data_dir="/data")
    assert reason_for("/archives", ctx) is ArchiveDirReason.DATA_DIR_OVERLAP


# --- Filesystem checks -------------------------------------------------------


def fs_ctx(**kwargs: object) -> ArchiveDirContext:
    return ArchiveDirContext(mounts=table(), **kwargs)  # type: ignore[arg-type]


@pytest.mark.usefixtures("tmp_allowed")
def test_filesystem_checks_accept_a_writable_directory(tmp_path: Path) -> None:
    assert reason_for(str(tmp_path), fs_ctx()) is None


@pytest.mark.usefixtures("tmp_allowed")
def test_filesystem_checks(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"x")
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    ctx = fs_ctx()
    assert reason_for(str(tmp_path / "missing"), ctx) is ArchiveDirReason.NOT_FOUND
    assert reason_for(str(tmp_path / "file"), ctx) is ArchiveDirReason.NOT_A_DIRECTORY
    assert reason_for(str(tmp_path / "link"), ctx) is ArchiveDirReason.SYMLINK
    assert reason_for(str(tmp_path / "link" / "sub"), ctx) in (
        ArchiveDirReason.SYMLINK,
        ArchiveDirReason.NOT_FOUND,
    )
    (tmp_path / "real" / "sub").mkdir()
    assert reason_for(str(tmp_path / "link" / "sub"), ctx) is ArchiveDirReason.SYMLINK


@non_root_only
@pytest.mark.usefixtures("tmp_allowed")
def test_read_only_directory_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "ro"
    target.mkdir(mode=0o500)
    try:
        assert reason_for(str(target), fs_ctx()) is ArchiveDirReason.NOT_WRITABLE
    finally:
        target.chmod(0o700)


@pytest.mark.usefixtures("tmp_allowed")
def test_overlap_uses_real_paths(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "root")
    ctx = fs_ctx(root_paths=(str(tmp_path / "alias"),))
    assert reason_for(str(tmp_path / "root"), ctx) is ArchiveDirReason.ROOT_OVERLAP


# --- Status from the configuration -------------------------------------------


def test_status_not_configured() -> None:
    config = parse_config({"data_dir": "/srv/treeaudit"})
    status = evaluate_archive_dir(config, table(), None)
    assert not status.enabled
    assert status.reason is ArchiveDirReason.NOT_CONFIGURED
    assert status.message == ARCHIVE_DIR_MESSAGES[ArchiveDirReason.NOT_CONFIGURED]


@pytest.mark.usefixtures("tmp_allowed")
def test_status_enabled_and_rejected(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    archives.mkdir()
    raw = {
        "data_dir": str(tmp_path / "data"),
        "archive_dir": str(archives),
        "roots": [{"id": "main", "path": str(tmp_path / "audit")}],
    }
    status = evaluate_archive_dir(parse_config(raw), table(), None)
    assert status.enabled
    assert status.message is None
    raw["roots"] = [{"id": "main", "path": str(tmp_path)}]
    status = evaluate_archive_dir(parse_config(raw), table(), None)
    assert status.reason is ArchiveDirReason.ROOT_OVERLAP
    assert not status.enabled
