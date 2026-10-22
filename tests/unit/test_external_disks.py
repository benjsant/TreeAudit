"""Detection of external disks (docs/DECISIONS.md D-033), on a fake sysfs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig, RootConfig
from treeaudit.roots import registry
from treeaudit.roots.external import (
    DISK_MESSAGES,
    DISKS_ENV_VAR,
    SYSTEM_DEVICES_ENV_VAR,
    DiskReason,
    ExternalDisk,
    check_mounted_disk,
    compose_override,
    default_media_dirs,
    disk_root_id,
    disks_from_json,
    disks_to_json,
    find_external_disks,
    format_device_specs,
    parse_device_specs,
    partitions,
    physical_disks,
    system_device_specs,
    system_disks,
    system_partitions,
)
from treeaudit.roots.mountinfo import MountTable, parse_mountinfo
from treeaudit.roots.registry import evaluate_roots, with_external_disks
from treeaudit.roots.validation import RejectReason

MEDIA = ["/run/media/me", "/media/me", "/media", "/mnt"]


def _block(sysfs: Path, device_dir: Path, major_minor: str) -> None:
    """Register a block device the way the kernel does (dev/block and class/block)."""
    for link in (
        sysfs / "dev" / "block" / major_minor,
        sysfs / "class" / "block" / device_dir.name,
    ):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(device_dir)


def _disk(sysfs: Path, bus: str, name: str, major_minor: str) -> Path:
    path = sysfs / "devices" / bus / name
    path.mkdir(parents=True)
    _block(sysfs, path, major_minor)
    return path


def _partition(disk: Path, sysfs: Path, name: str, major_minor: str) -> Path:
    path = disk / name
    path.mkdir()
    (path / "partition").write_text("1\n")
    _block(sysfs, path, major_minor)
    return path


def _mapper(sysfs: Path, name: str, mapper: str, major_minor: str, slave: Path) -> None:
    path = sysfs / "devices" / "virtual" / "block" / name
    (path / "dm").mkdir(parents=True)
    (path / "dm" / "name").write_text(mapper + "\n")
    (path / "slaves").mkdir()
    (path / "slaves" / slave.name).symlink_to(slave)
    _block(sysfs, path, major_minor)


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
    root = tmp_path / "sys"
    nvme = _disk(root, "pci/nvme0", "nvme0n1", "259:0")
    for number in range(1, 6):
        _partition(nvme, root, f"nvme0n1p{number}", f"259:{number}")
    sdb = _disk(root, "usb1", "sdb", "8:16")
    _partition(sdb, root, "sdb1", "8:17")
    _disk(root, "usb2", "sdc", "8:32")  # filesystem on the whole disk
    sdd = _disk(root, "usb3", "sdd", "8:48")
    sdd2 = _partition(sdd, root, "sdd2", "8:50")
    _mapper(root, "dm-0", "luks-usb", "253:0", sdd2)
    _mapper(root, "dm-1", "luks-sys", "253:1", root / "devices/pci/nvme0/nvme0n1/nvme0n1p5")
    _mapper(root, "dm-2", "luks-root", "253:2", root / "devices/pci/nvme0/nvme0n1/nvme0n1p3")
    loop = root / "devices" / "virtual" / "block" / "loop0"
    loop.mkdir(parents=True)
    _block(root, loop, "7:0")
    return root


HOST = b"""\
1 0 259:3 / / rw,relatime - ext4 /dev/nvme0n1p3 rw
2 1 0:35 /home /home rw,relatime - btrfs /dev/nvme0n1p3 rw
3 1 259:1 / /boot/efi rw - vfat /dev/nvme0n1p1 rw
4 1 0:21 / /proc rw - proc proc rw
10 1 259:4 / /mnt/Data rw,relatime - ext4 /dev/nvme0n1p4 rw
11 1 8:17 / /run/media/me/USB\\040KEY rw,nosuid - exfat /dev/sdb1 rw
12 1 8:32 / /run/media/me/Whole rw - ext4 /dev/sdc rw
13 1 253:0 / /run/media/me/Crypt rw - ext4 /dev/mapper/luks-usb rw
14 1 253:1 / /run/media/me/SysCrypt rw - ext4 /dev/mapper/luks-sys rw
15 1 7:0 / /run/media/me/Iso ro - iso9660 /dev/loop0 ro
16 1 0:50 / /media/me/Btrfs rw - btrfs /dev/sdb1 rw
17 1 0:60 / /mnt/Net rw - fuse.sshfs me@nas:/ rw
18 11 8:18 / /run/media/me/USB\\040KEY/nested rw - ext4 /dev/sdb2 rw
19 1 0:70 / /mnt/tmp rw - sysfs sysfs rw
"""


@pytest.fixture
def host() -> MountTable:
    return parse_mountinfo(HOST)


def detect(host: MountTable, sysfs: Path, **kwargs: object) -> dict[str, DiskReason | None]:
    system = system_disks(system_device_specs(host), sysfs)
    disks = find_external_disks(MEDIA, host, system, sysfs=sysfs, **kwargs)  # type: ignore[arg-type]
    return {disk.mount_point: disk.reason for disk in disks}


def test_every_reason_has_a_message() -> None:
    assert set(DISK_MESSAGES) == set(DiskReason)


def test_physical_disks(sysfs: Path) -> None:
    assert physical_disks("259:3", "/dev/nvme0n1p3", sysfs) == {"nvme0n1"}
    assert physical_disks("8:32", "/dev/sdc", sysfs) == {"sdc"}
    assert physical_disks("253:0", "/dev/mapper/luks-usb", sysfs) == {"sdd"}
    # btrfs reports an anonymous device number: found through the source.
    assert physical_disks("0:50", "/dev/sdb1", sysfs) == {"sdb"}
    assert physical_disks("0:51", "/dev/mapper/luks-sys", sysfs) == {"nvme0n1"}
    with pytest.raises(LookupError):
        physical_disks("0:60", "me@nas:/", sysfs)
    with pytest.raises(LookupError):
        physical_disks("0:61", "/dev/../sda", sysfs)


def test_system_disks(host: MountTable, sysfs: Path) -> None:
    specs = system_device_specs(host)
    assert ("259:3", "/dev/nvme0n1p3") in specs
    assert system_disks(specs, sysfs) == {"nvme0n1"}
    assert system_disks([], sysfs) is None
    assert system_disks([("9:9", "/dev/unknown")], sysfs) is None


def test_device_specs_round_trip() -> None:
    specs = [("259:3", "/dev/nvme0n1p3"), ("0:35", "/dev/mapper/luks-a_b.c")]
    assert parse_device_specs(format_device_specs(specs)) == specs
    assert parse_device_specs("259:3|/dev/x;rm,8:1|relative,1:2|/dev/ok") == [("1:2", "/dev/ok")]


def test_detection(host: MountTable, sysfs: Path) -> None:
    assert detect(host, sysfs) == {
        "/mnt/Data": DiskReason.SYSTEM_DISK,  # another partition of the system disk
        "/mnt/Net": DiskReason.UNKNOWN_DISK,  # network filesystem: no physical disk
        "/media/me/Btrfs": None,
        "/run/media/me/Crypt": None,  # LUKS on an external disk
        "/run/media/me/Iso": DiskReason.DISK_IMAGE,
        "/run/media/me/SysCrypt": DiskReason.SYSTEM_DISK,  # LUKS on the system disk
        "/run/media/me/USB KEY": None,
        "/run/media/me/Whole": None,
    }  # nested mounts and virtual filesystems are not candidates


def test_unknown_system_disk_offers_nothing(host: MountTable, sysfs: Path) -> None:
    disks = find_external_disks(MEDIA, host, None, sysfs=sysfs)
    assert disks and all(d.reason is DiskReason.SYSTEM_UNKNOWN for d in disks)


def test_disk_holding_the_data_dir_is_offered_and_flagged(host: MountTable, sysfs: Path) -> None:
    """D-037: TreeAudit's own folder is left out of the analysis by identity instead."""
    system = system_disks(system_device_specs(host), sysfs)
    disks = find_external_disks(
        MEDIA, host, system, sysfs=sysfs, data_dir="/run/media/me/Whole/treeaudit/data"
    )
    by_point = {disk.mount_point: disk for disk in disks}
    assert by_point["/run/media/me/Whole"].accepted
    assert by_point["/run/media/me/Whole"].holds_treeaudit
    assert not by_point["/run/media/me/Crypt"].holds_treeaudit


# --- data partitions of the system disk (D-037) ------------------------------------------


INTERNAL_HOST = HOST + (
    b"20 1 259:3 /srv /mnt/RootAgain rw - ext4 /dev/nvme0n1p3 rw\n"
    b"21 1 253:2 / /mnt/RootCrypt rw - ext4 /dev/mapper/luks-root rw\n"
)


def test_partitions(sysfs: Path) -> None:
    assert partitions("259:4", "/dev/nvme0n1p4", sysfs) == {"nvme0n1p4"}
    assert partitions("253:1", "/dev/mapper/luks-sys", sysfs) == {"nvme0n1p5"}
    assert partitions("8:32", "/dev/sdc", sysfs) == {"sdc"}  # no partition table
    specs = system_device_specs(parse_mountinfo(HOST))
    assert system_partitions(specs, sysfs) == {"nvme0n1p1", "nvme0n1p3"}
    assert system_partitions([("9:9", "/dev/unknown")], sysfs) is None


def test_internal_partitions_are_offered_never_the_systems(sysfs: Path) -> None:
    table = parse_mountinfo(INTERNAL_HOST)
    specs = system_device_specs(table)
    disks = find_external_disks(
        MEDIA,
        table,
        system_disks(specs, sysfs),
        sysfs=sysfs,
        system_parts=system_partitions(specs, sysfs),
    )
    found = {disk.mount_point: (disk.reason, disk.internal) for disk in disks}
    assert found["/mnt/Data"] == (None, True)  # a data partition of the system disk
    assert found["/run/media/me/SysCrypt"] == (None, True)  # LUKS on such a partition
    # The system's own filesystem, mounted again or through its LUKS container.
    assert found["/mnt/RootAgain"] == (DiskReason.SYSTEM_PARTITION, True)
    assert found["/mnt/RootCrypt"] == (DiskReason.SYSTEM_PARTITION, True)
    assert found["/run/media/me/USB KEY"] == (None, False)
    # Without the system's partitions (option off, or unknown): nothing internal.
    assert detect(table, sysfs)["/mnt/Data"] is DiskReason.SYSTEM_DISK


def test_names_with_control_characters_are_refused(sysfs: Path) -> None:
    table = parse_mountinfo(
        HOST.split(b"10 1")[0] + b"20 1 8:32 / /run/media/me/a\\012b rw - ext4 /dev/sdc rw\n"
    )
    assert detect(table, sysfs) == {"/run/media/me/a\nb": DiskReason.UNSAFE_NAME}


def test_root_ids_are_stable_and_valid() -> None:
    first = disk_root_id("/run/media/me/USB KEY")
    assert first == disk_root_id("/run/media/me/USB KEY")
    assert first != disk_root_id("/media/me/USB KEY")
    for path in ("/run/media/me/USB KEY", "/mnt/$(id)", "/mnt/ÉTÉ", "/mnt/" + "x" * 300):
        assert ROOT_ID_PATTERN.fullmatch(disk_root_id(path)), path
        assert disk_root_id(path).startswith("ext-")


def test_default_media_dirs() -> None:
    assert default_media_dirs("me") == ["/run/media/me", "/media/me", "/media", "/mnt"]
    assert default_media_dirs("../x") == ["/media", "/mnt"]
    assert default_media_dirs(None) == ["/media", "/mnt"]


# --- hand-over to the container --------------------------------------------------------------


def test_json_round_trip_and_tampering(host: MountTable, sysfs: Path) -> None:
    system = system_disks(system_device_specs(host), sysfs)
    disks = find_external_disks(MEDIA, host, system, sysfs=sysfs)
    assert disks_from_json(disks_to_json(disks)) == disks
    flagged = [ExternalDisk("D", "/mnt/D", disk_root_id("/mnt/D"), "ext4", None, True, True)]
    assert disks_from_json(disks_to_json(flagged)) == flagged
    bad_flag = json.dumps([{**json.loads(disks_to_json(flagged))[0], "internal": "yes"}])
    assert disks_from_json(bad_flag) == []
    forged = json.dumps([{"id": "ext-x-000000", "name": "x", "host_path": "/", "reason": None}])
    assert disks_from_json(forged) == []
    assert disks_from_json("not json") == []
    assert disks_from_json('{"id": 1}') == []


def test_compose_override_mounts_only_accepted_disks_read_only(
    host: MountTable, sysfs: Path
) -> None:
    system = system_disks(system_device_specs(host), sysfs)
    disks = [
        *find_external_disks(MEDIA, host, system, sysfs=sysfs),
        ExternalDisk("$HOME", "/mnt/$HOME", disk_root_id("/mnt/$HOME"), "ext4"),
    ]
    override = json.loads(compose_override(disks, "259:3|/dev/nvme0n1p3"))
    assert set(override["services"]) == {"app"}
    app = override["services"]["app"]
    sources = {volume["source"] for volume in app["volumes"]}
    assert sources == {
        "/media/me/Btrfs",
        "/run/media/me/Crypt",
        "/run/media/me/USB KEY",
        "/run/media/me/Whole",
        "/mnt/$$HOME",  # '$' doubled: compose never interpolates a disk name
    }
    for volume in app["volumes"]:
        assert volume["read_only"] is True
        assert volume["bind"] == {"create_host_path": False, "recursive": "readonly"}
        assert volume["target"].startswith("/audit/disks/ext-")
    assert app["environment"][SYSTEM_DEVICES_ENV_VAR] == "259:3|/dev/nvme0n1p3"
    assert "$HOME" not in app["environment"][DISKS_ENV_VAR].replace("$$", "")


# --- inside the container -----------------------------------------------------------------------


def test_check_mounted_disk(sysfs: Path) -> None:
    table = parse_mountinfo(
        b"1 0 0:99 / / rw - overlay overlay rw\n"
        b"2 1 8:17 / /audit/disks/ext-a ro - exfat /dev/sdb1 rw\n"
        b"3 1 8:32 / /audit/disks/ext-b rw - ext4 /dev/sdc rw\n"
        b"4 1 259:4 / /audit/disks/ext-c ro - ext4 /dev/nvme0n1p4 rw\n"
    )
    system = frozenset({"nvme0n1"})
    assert check_mounted_disk("/audit/disks/ext-a", table, system, sysfs) is None
    assert (
        check_mounted_disk("/audit/disks/ext-b", table, system, sysfs) is DiskReason.NOT_READ_ONLY
    )
    assert check_mounted_disk("/audit/disks/ext-c", table, system, sysfs) is DiskReason.SYSTEM_DISK
    assert check_mounted_disk("/audit/disks/ext-d", table, system, sysfs) is DiskReason.NOT_MOUNTED


def test_check_mounted_internal_disk(sysfs: Path) -> None:
    table = parse_mountinfo(
        b"1 0 0:99 / / rw - overlay overlay rw\n"
        b"2 1 259:4 / /audit/disks/ext-c ro - ext4 /dev/nvme0n1p4 rw\n"
        b"3 1 259:3 / /audit/disks/ext-r ro - ext4 /dev/nvme0n1p3 rw\n"
        b"4 1 8:17 / /audit/disks/ext-a ro - exfat /dev/sdb1 rw\n"
    )
    system, parts = frozenset({"nvme0n1"}), frozenset({"nvme0n1p1", "nvme0n1p3"})

    def check(path: str, *, internal: bool, system_parts: frozenset[str] | None = parts) -> object:
        return check_mounted_disk(
            path, table, system, sysfs, system_parts=system_parts, internal=internal
        )

    assert check("/audit/disks/ext-c", internal=True) is None
    # Announced as a separate disk, a partition of the system disk is refused.
    assert check("/audit/disks/ext-c", internal=False) is DiskReason.SYSTEM_DISK
    assert check("/audit/disks/ext-r", internal=True) is DiskReason.SYSTEM_PARTITION
    assert check("/audit/disks/ext-c", internal=True, system_parts=None) is DiskReason.SYSTEM_DISK
    assert check("/audit/disks/ext-a", internal=True) is None  # a real external disk


def test_container_roots_come_from_the_host_detection(
    tmp_path: Path, sysfs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = ExternalDisk("USB", "/run/media/me/USB", disk_root_id("/run/media/me/USB"), "exfat")
    moved = ExternalDisk("Old", "/run/media/me/Old", disk_root_id("/run/media/me/Old"), "ext4")
    refused = ExternalDisk(
        "Data", "/mnt/Data", disk_root_id("/mnt/Data"), "ext4", DiskReason.SYSTEM_DISK
    )
    environ = {
        DISKS_ENV_VAR: disks_to_json([good, moved, refused]),
        SYSTEM_DEVICES_ENV_VAR: "259:3|/dev/nvme0n1p3",
    }
    config = AppConfig(data_dir="/data", roots=(), container_mode=True)
    config, disks = with_external_disks(config, None, environ, sysfs=sysfs)
    assert [d.name for d in disks] == ["USB", "Old", "Data"]
    assert [(r.label, r.display_path, r.external) for r in config.roots] == [
        ("USB", "/run/media/me/USB", True),
        ("Old", "/run/media/me/Old", True),
    ]
    assert config.roots[0].path == f"/audit/disks/{good.root_id}"
    assert config.declared_roots == ()

    # Inside the container the first disk is mounted read-only; the second is
    # now the system disk (swapped since the host detection): refused again.
    table = parse_mountinfo(
        f"1 0 0:99 / / rw - overlay overlay rw\n"
        f"2 1 8:17 / /audit/disks/{good.root_id} ro - exfat /dev/sdb1 rw\n"
        f"3 1 259:4 / /audit/disks/{moved.root_id} ro - ext4 /dev/nvme0n1p4 rw\n".encode()
    )
    # The container paths do not exist here: only the disk re-check is exercised.
    monkeypatch.setattr(registry, "validate_configured_root", lambda *args, **kwargs: None)
    statuses = evaluate_roots(config, table, home=None, environ=environ, sysfs=sysfs)
    assert [s.reason for s in statuses] == [None, RejectReason.NOT_EXTERNAL_DISK]


def test_native_detection_adds_roots(host: MountTable, sysfs: Path) -> None:
    declared = RootConfig(id="main", path="/srv/backup", label="main")
    config = AppConfig(data_dir="/srv/treeaudit", roots=(declared,), external_disks=True)
    config, disks = with_external_disks(config, host, {}, sysfs=sysfs, user="me")
    assert config.declared_roots == (declared,)
    external = [r for r in config.roots if r.external]
    assert {r.path for r in external} == {d.mount_point for d in disks if d.accepted}
    assert len(external) == 6
    assert {r.path for r in external if r.internal} == {"/mnt/Data", "/run/media/me/SysCrypt"}
    disabled = AppConfig(data_dir="/srv/treeaudit", roots=(declared,))
    assert with_external_disks(disabled, host, {}, sysfs=sysfs, user="me") == (disabled, [])
