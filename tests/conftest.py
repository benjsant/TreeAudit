"""Shared fixtures and sample mount tables.

Samples are hand-written after typical layouts (ext4 as on Linux Mint, btrfs
subvolumes as on Fedora, a Docker container); tests never depend on the mounts of
the machine running them.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from treeaudit.roots.mountinfo import MountEntry, MountTable, parse_mountinfo

# Classic layout: ext4 system partition holding /home, data disk on another partition.
EXT4_HOST = b"""\
22 1 259:3 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p3 rw
23 22 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
24 22 0:22 / /sys rw,nosuid,nodev,noexec,relatime shared:2 - sysfs sysfs rw
25 22 0:5 / /dev rw,nosuid shared:8 - devtmpfs devtmpfs rw,size=4096k,mode=755
26 22 0:25 / /run rw,nosuid,nodev shared:14 - tmpfs tmpfs rw,mode=755
27 22 0:30 / /tmp rw,nosuid,nodev shared:16 - tmpfs tmpfs rw
28 24 0:26 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime shared:4 - cgroup2 cgroup2 rw
40 22 259:4 / /mnt/Data rw,relatime shared:30 - ext4 /dev/nvme0n1p4 rw
41 26 8:17 / /run/media/user/USB\\040KEY rw,nosuid,nodev,relatime shared:40 - exfat /dev/sdb1 rw
"""

# Fedora default: one btrfs filesystem, several subvolumes. /srv/data shows a
# different device number although it lives on the same filesystem.
BTRFS_HOST = b"""\
60 1 0:35 /root / rw,relatime shared:1 - btrfs /dev/nvme0n1p3 rw,subvol=/root
61 60 0:35 /home /home rw,relatime shared:2 - btrfs /dev/nvme0n1p3 rw,subvol=/home
62 60 0:40 /data /srv/data rw,relatime shared:3 - btrfs /dev/nvme0n1p3 rw,subvol=/data
63 60 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
70 60 8:1 / /mnt/backup rw,relatime shared:50 - btrfs /dev/sda1 rw,subvol=/
"""

# Inside the TreeAudit container: overlay root, audited folder bind-mounted.
CONTAINER = b"""\
500 400 0:60 / / rw,relatime - overlay overlay rw,lowerdir=/l,upperdir=/u,workdir=/w
501 500 259:4 /Backup /audit/roots/main ro,relatime - ext4 /dev/nvme0n1p4 rw
502 500 259:4 /Other /audit/roots/writable rw,relatime - ext4 /dev/nvme0n1p4 rw
503 500 0:61 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
504 500 0:62 / /data rw,relatime - ext4 /dev/nvme0n1p4 rw
"""


@pytest.fixture
def ext4_host() -> MountTable:
    return parse_mountinfo(EXT4_HOST)


@pytest.fixture
def btrfs_host() -> MountTable:
    return parse_mountinfo(BTRFS_HOST)


@pytest.fixture
def container_mounts() -> MountTable:
    return parse_mountinfo(CONTAINER)


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


TableFactory = Callable[..., MountTable]


@pytest.fixture
def data_disk_table() -> TableFactory:
    """Factory: system on 259:3, and a separate data disk mounted at the given point."""

    def factory(data_mount_point: str, **kwargs: str) -> MountTable:
        return MountTable(
            entries=(
                make_entry("/", major_minor="259:3", source="/dev/nvme0n1p3", mount_id=1),
                make_entry(data_mount_point, major_minor="8:1", mount_id=2, **kwargs),
            )
        )

    return factory
