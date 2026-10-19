from __future__ import annotations

import pytest

from treeaudit.roots.mountinfo import MountInfoError, MountTable, parse_mountinfo


def test_parses_fields(ext4_host: MountTable) -> None:
    root = ext4_host.entries[0]
    assert root.mount_id == 22
    assert root.parent_id == 1
    assert root.major_minor == "259:3"
    assert root.fs_root == "/"
    assert root.mount_point == "/"
    assert root.fs_type == "ext4"
    assert root.source == "/dev/nvme0n1p3"
    assert "rw" in root.mount_options
    assert not root.is_read_only


def test_unescapes_octal_sequences(ext4_host: MountTable) -> None:
    points = [entry.mount_point for entry in ext4_host.entries]
    assert "/run/media/user/USB KEY" in points


def test_line_without_optional_fields() -> None:
    table = parse_mountinfo(b"1 0 8:1 / /mnt rw - ext4 /dev/sda1 rw\n")
    assert table.entries[0].fs_type == "ext4"
    assert table.entries[0].source == "/dev/sda1"


def test_line_with_several_optional_fields() -> None:
    line = b"1 0 8:1 / /mnt rw shared:1 master:2 propagate_from:3 - ext4 /dev/sda1 rw\n"
    assert parse_mountinfo(line).entries[0].fs_type == "ext4"


def test_read_only_and_virtual_flags(container_mounts: MountTable) -> None:
    by_point = {entry.mount_point: entry for entry in container_mounts.entries}
    assert by_point["/audit/roots/main"].is_read_only
    assert not by_point["/audit/roots/writable"].is_read_only
    assert by_point["/proc"].is_virtual
    assert not by_point["/"].is_virtual


@pytest.mark.parametrize(
    ("path", "expected_point"),
    [
        ("/", "/"),
        ("/etc/passwd", "/"),
        ("/mnt/Data", "/mnt/Data"),
        ("/mnt/Data/a/b", "/mnt/Data"),
        ("/mnt/DataX", "/"),  # prefix must stop at a path separator
        ("/run/media/user/USB KEY/photos", "/run/media/user/USB KEY"),
        ("/sys/fs/cgroup/x", "/sys/fs/cgroup"),
    ],
)
def test_mount_for_longest_prefix(ext4_host: MountTable, path: str, expected_point: str) -> None:
    entry = ext4_host.mount_for(path)
    assert entry is not None
    assert entry.mount_point == expected_point


def test_stacked_mounts_last_one_wins() -> None:
    table = parse_mountinfo(
        b"1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        b"2 1 8:2 / /mnt rw - ext4 /dev/sda2 rw\n"
        b"3 1 8:3 / /mnt rw - ext4 /dev/sda3 rw\n"
    )
    entry = table.mount_for("/mnt/x")
    assert entry is not None
    assert entry.source == "/dev/sda3"


def test_mount_for_without_root_mount() -> None:
    table = parse_mountinfo(b"2 1 8:2 / /mnt rw - ext4 /dev/sda2 rw\n")
    assert table.mount_for("/etc") is None


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n\n",
        b"1 0 8:1 / / rw ext4 /dev/sda1 rw\n",  # no separator
        b"x 0 8:1 / / rw - ext4 /dev/sda1 rw\n",  # bad id
        b"1 0 8:1 / / rw -\n",  # truncated
    ],
)
def test_invalid_content_raises(content: bytes) -> None:
    with pytest.raises(MountInfoError):
        parse_mountinfo(content)
