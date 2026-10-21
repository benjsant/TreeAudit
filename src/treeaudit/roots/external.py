"""Detection of external disks that may be audited (docs/DECISIONS.md D-033).

Desktop Linux mounts secondary and removable disks below a few folders
(/run/media/<user> on Fedora, /media/<user> on Debian and Mint, /media, /mnt
for disks declared in fstab). When the option is enabled, every disk mounted
directly below one of them is a candidate root, accepted only if it is **not
on a physical disk of the running system** and if its physical disk can be
determined at all. These folder names only say where to look; the decision
rests on the physical disk.

The physical disk is found through sysfs, read only: the mount's device number
(or its /dev source) leads to a block device, then through device-mapper and
RAID layers (``slaves/``: LUKS, LVM, md) and partitions (``partition``) to
whole disks. Anything that cannot be resolved (network or FUSE filesystems,
disk images on loop devices, unknown layouts) is refused: in doubt, a disk is
not offered.

In Docker the detection runs on the host, before anything is mounted (like the
validation of the root, D-008), and only accepted disks are mounted, one by
one and read-only: the container never sees the system disk nor a refused one.
Inside the container each disk is checked again (``check_mounted_disk``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from treeaudit.roots.mountinfo import MountEntry, MountTable
from treeaudit.roots.validation import is_same_or_parent

SYSFS = Path("/sys")
SYSTEM_DEVICES_ENV_VAR = "TREEAUDIT_SYSTEM_DEVICES"
DISKS_ENV_VAR = "TREEAUDIT_EXTERNAL_DISKS"
CONTAINER_DISKS_DIR = "/audit/disks"

# Mount points whose disks belong to the running system.
SYSTEM_MOUNT_POINTS = ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var")

# Block devices that are not physical disks of their own.
_IMAGE_PREFIXES = ("loop", "ram", "zram", "nbd")
_MAX_LAYERS = 8
_DEVICE_SPEC = re.compile(r"(\d+:\d+)\|(/dev/[A-Za-z0-9._/:+-]+)")
_ID_CHARS = re.compile(r"[^a-z0-9]+")


class DiskReason(StrEnum):
    SYSTEM_DISK = "system_disk"
    UNKNOWN_DISK = "unknown_disk"
    DISK_IMAGE = "disk_image"
    SYSTEM_UNKNOWN = "system_unknown"
    DATA_DIR_INSIDE = "data_dir_inside"
    UNSAFE_NAME = "unsafe_name"
    NOT_MOUNTED = "not_mounted"
    NOT_READ_ONLY = "not_read_only"


# User-facing explanations (French, see docs/DECISIONS.md D-016).
DISK_MESSAGES: dict[DiskReason, str] = {
    DiskReason.SYSTEM_DISK: "ce disque contient le système en cours d'exécution",
    DiskReason.UNKNOWN_DISK: (
        "impossible de savoir sur quel disque physique il se trouve : par prudence, "
        "il n'est pas proposé"
    ),
    DiskReason.DISK_IMAGE: "c'est une image disque ou un disque virtuel, pas un disque physique",
    DiskReason.SYSTEM_UNKNOWN: (
        "le disque du système n'a pas pu être identifié : par prudence, aucun disque n'est proposé"
    ),
    DiskReason.DATA_DIR_INSIDE: "il contient le dossier de données de TreeAudit",
    DiskReason.UNSAFE_NAME: (
        "son nom contient des caractères de contrôle : il ne peut pas être affiché sans ambiguïté"
    ),
    DiskReason.NOT_MOUNTED: "il n'est plus monté (débranché ?) : relancer ./treeaudit",
    DiskReason.NOT_READ_ONLY: "il n'est pas en lecture seule dans le conteneur",
}


class _Image(Exception):
    """The block device is a disk image or a virtual device."""


@dataclass(frozen=True, slots=True)
class ExternalDisk:
    """A disk mounted below one of the media folders, offered or refused."""

    name: str
    """Name of its mount point (usually the disk label), as shown to the user."""
    mount_point: str
    """Path on the machine that ran the detection (the host in Docker)."""
    root_id: str
    fs_type: str
    reason: DiskReason | None = None

    @property
    def accepted(self) -> bool:
        return self.reason is None

    @property
    def message(self) -> str | None:
        return None if self.reason is None else DISK_MESSAGES[self.reason]


def default_media_dirs(user: str | None) -> list[str]:
    """Folders below which desktop Linux mounts secondary disks."""
    dirs = []
    if user and "/" not in user and user not in (".", ".."):
        dirs += [f"/run/media/{user}", f"/media/{user}"]
    return [*dirs, "/media", "/mnt"]


def disk_root_id(mount_point: str) -> str:
    """Stable root id: same mount point, same id (its analyses stay grouped)."""
    name = os.path.basename(mount_point)
    slug = _ID_CHARS.sub("-", name.lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha256(os.fsencode(mount_point)).hexdigest()[:6]
    return f"ext-{slug}-{digest}" if slug else f"ext-{digest}"


# --- physical disks through sysfs ----------------------------------------------------


def _block_node(major_minor: str, source: str, sysfs: Path) -> Path | None:
    """sysfs directory of the block device behind a mount, if it can be found."""
    if not major_minor.startswith("0:"):  # 0:N are anonymous (btrfs, tmpfs, FUSE...)
        node = sysfs / "dev" / "block" / major_minor
        if node.exists():
            return node.resolve()
    if source.startswith("/dev/mapper/"):
        wanted = source.removeprefix("/dev/mapper/")
        for candidate in sorted((sysfs / "class" / "block").glob("dm-*")):
            try:
                name = (candidate / "dm" / "name").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if name == wanted:
                return candidate.resolve()
        return None
    if source.startswith("/dev/"):
        name = source.removeprefix("/dev/")
        if name and "/" not in name and name not in (".", ".."):
            node = sysfs / "class" / "block" / name
            if node.exists():
                return node.resolve()
    return None


def _disks_of(node: Path, depth: int = 0) -> frozenset[str]:
    """Whole physical disks under a block device. Raises _Image or LookupError."""
    if depth > _MAX_LAYERS:
        raise LookupError("too many device layers")
    if node.name.startswith(_IMAGE_PREFIXES):
        raise _Image
    slaves = node / "slaves"
    if slaves.is_dir():
        layers = sorted(slaves.iterdir())
        if layers:  # device-mapper (LUKS, LVM) or md RAID: the disks underneath
            disks: set[str] = set()
            for layer in layers:
                disks |= _disks_of(layer.resolve(), depth + 1)
            return frozenset(disks)
    if (node / "partition").exists():
        return _disks_of(node.parent, depth + 1)
    if node.name.startswith(("dm-", "md")):
        raise LookupError("virtual device without underlying disks")
    return frozenset({node.name})


def physical_disks(major_minor: str, source: str, sysfs: Path = SYSFS) -> frozenset[str]:
    """Physical disks holding a filesystem. Raises _Image or LookupError if unknown."""
    node = _block_node(major_minor, source, sysfs)
    if node is None:
        raise LookupError(f"no block device for {major_minor} {source}")
    return _disks_of(node)


# --- the running system's disks -------------------------------------------------------


def system_device_specs(mounts: MountTable) -> list[tuple[str, str]]:
    """(major:minor, source) of the system mount points found in ``mounts``."""
    specs: list[tuple[str, str]] = []
    for point in SYSTEM_MOUNT_POINTS:
        entry = _exact_mount(mounts, point)
        if entry is not None:
            specs.append((entry.major_minor, entry.source))
    return specs


def format_device_specs(specs: Iterable[tuple[str, str]]) -> str:
    return ",".join(f"{major_minor}|{source}" for major_minor, source in specs)


def parse_device_specs(text: str) -> list[tuple[str, str]]:
    """Parse ``major:minor|/dev/x,...``; anything malformed is ignored."""
    specs: list[tuple[str, str]] = []
    for item in text.split(","):
        match = _DEVICE_SPEC.fullmatch(item.strip())
        if match is not None:
            specs.append((match.group(1), match.group(2)))
    return specs


def system_disks(specs: Iterable[tuple[str, str]], sysfs: Path = SYSFS) -> frozenset[str] | None:
    """Physical disks of the system; None if there is none or one cannot be resolved.

    A system filesystem whose disk is unknown makes every other disk suspicious:
    the caller then offers none.
    """
    disks: set[str] = set()
    found = False
    for major_minor, source in specs:
        found = True
        try:
            disks |= physical_disks(major_minor, source, sysfs)
        except (LookupError, _Image, OSError):
            return None
    return frozenset(disks) if found else None


def _exact_mount(mounts: MountTable, point: str) -> MountEntry | None:
    visible = None
    for entry in mounts.entries:
        if entry.mount_point == point:
            visible = entry  # the last one listed is the visible one
    return visible


# --- candidates -------------------------------------------------------------------------


def find_external_disks(
    media_dirs: Iterable[str],
    mounts: MountTable,
    system: frozenset[str] | None,
    *,
    data_dir: str | None = None,
    sysfs: Path = SYSFS,
) -> list[ExternalDisk]:
    """Every disk mounted directly below one of ``media_dirs``, offered or refused.

    All paths belong to the machine whose ``mounts`` and ``sysfs`` are given
    (the host, even when this runs in a container). ``system`` is the set of
    the system's physical disks.
    """
    latest: dict[str, MountEntry] = {}
    for media_dir in media_dirs:
        prefix = media_dir.rstrip("/") + "/"
        for entry in mounts.entries:
            point = entry.mount_point
            if point.startswith(prefix) and "/" not in point[len(prefix) :]:
                latest[point] = entry  # stacked mounts: the last one is visible
    disks: list[ExternalDisk] = []
    for point, entry in sorted(latest.items()):
        if entry.is_virtual:
            continue
        disks.append(
            ExternalDisk(
                name=os.path.basename(point),
                mount_point=point,
                root_id=disk_root_id(point),
                fs_type=entry.fs_type,
                reason=_refusal(entry, system, data_dir, sysfs),
            )
        )
    return disks


def _refusal(
    entry: MountEntry, system: frozenset[str] | None, data_dir: str | None, sysfs: Path
) -> DiskReason | None:
    if any(ord(char) < 32 or ord(char) == 127 for char in entry.mount_point):
        return DiskReason.UNSAFE_NAME
    reason = _disk_refusal(entry, system, sysfs)
    if reason is not None:
        return reason
    if data_dir is not None and is_same_or_parent(entry.mount_point, os.path.normpath(data_dir)):
        return DiskReason.DATA_DIR_INSIDE
    return None


def _disk_refusal(
    entry: MountEntry, system: frozenset[str] | None, sysfs: Path
) -> DiskReason | None:
    if system is None:
        return DiskReason.SYSTEM_UNKNOWN
    try:
        disks = physical_disks(entry.major_minor, entry.source, sysfs)
    except _Image:
        return DiskReason.DISK_IMAGE
    except (LookupError, OSError):
        return DiskReason.UNKNOWN_DISK
    if disks & system:
        return DiskReason.SYSTEM_DISK
    return None


def check_mounted_disk(
    path: str, mounts: MountTable, system: frozenset[str] | None, sysfs: Path = SYSFS
) -> DiskReason | None:
    """Second check inside the container: ``path`` must be a dedicated, read-only
    mount of a disk that is still not a system disk."""
    entry = _exact_mount(mounts, path)
    if entry is None:
        return DiskReason.NOT_MOUNTED
    if not entry.is_read_only:
        return DiskReason.NOT_READ_ONLY
    return _disk_refusal(entry, system, sysfs)


# --- hand-over from the host to the container ----------------------------------------------


def disks_to_json(disks: Iterable[ExternalDisk]) -> str:
    """What the container needs to know: ids, names, host paths, refusals."""
    return json.dumps(
        [
            {
                "id": disk.root_id,
                "name": disk.name,
                "host_path": disk.mount_point,
                "fs_type": disk.fs_type,
                "reason": None if disk.reason is None else disk.reason.value,
            }
            for disk in disks
        ],
        ensure_ascii=True,
    )


def disks_from_json(text: str) -> list[ExternalDisk]:
    """Inverse of disks_to_json; malformed items are dropped."""
    try:
        items: Any = json.loads(text)
    except ValueError:
        return []
    disks: list[ExternalDisk] = []
    if not isinstance(items, list):
        return disks
    for item in items:
        if not isinstance(item, dict):
            continue
        root_id, name, host_path = item.get("id"), item.get("name"), item.get("host_path")
        fs_type, reason = item.get("fs_type", ""), item.get("reason")
        if not (
            isinstance(root_id, str)
            and isinstance(name, str)
            and isinstance(host_path, str)
            and isinstance(fs_type, str)
        ):
            continue
        if not host_path.startswith("/") or root_id != disk_root_id(host_path):
            continue
        try:
            parsed = None if reason is None else DiskReason(reason)
        except ValueError:
            continue
        disks.append(ExternalDisk(name, host_path, root_id, fs_type, parsed))
    return disks


def disks_from_environment(environ: Mapping[str, str]) -> list[ExternalDisk]:
    return disks_from_json(environ.get(DISKS_ENV_VAR, "[]"))


def system_specs_from_environment(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """In Docker, the host's system devices, measured on the host before starting."""
    return parse_device_specs(environ.get(SYSTEM_DEVICES_ENV_VAR, ""))


def compose_override(disks: Iterable[ExternalDisk], system_specs: str) -> str:
    """Compose file mounting each accepted disk read-only (JSON is valid YAML).

    Every value is JSON-escaped; '$' is doubled so that compose never
    interpolates a disk name as a variable.
    """
    disks = list(disks)
    volumes = [
        {
            "type": "bind",
            "source": disk.mount_point.replace("$", "$$"),
            "target": f"{CONTAINER_DISKS_DIR}/{disk.root_id}",
            "read_only": True,
            "bind": {"create_host_path": False, "recursive": "readonly"},
        }
        for disk in disks
        if disk.accepted
    ]
    service = {
        "environment": {
            DISKS_ENV_VAR: disks_to_json(disks).replace("$", "$$"),
            SYSTEM_DEVICES_ENV_VAR: system_specs,
        },
        "volumes": volumes,
    }
    return json.dumps({"services": {"app": service}}, indent=2, ensure_ascii=True) + "\n"
