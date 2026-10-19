"""Parsing of ``/proc/self/mountinfo`` and lookup of the mount holding a path.

Format (proc(5)), one mount per line:

    36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 - ext3 /dev/root rw,errors=continue
    (1)(2)(3)   (4)   (5)         (6)       (7)      (8)(9)  (10)      (11)

Fields 7 are zero or more optional fields terminated by a single ``-``.
Spaces, tabs, newlines and backslashes in paths are escaped as octal (``\\040``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

MOUNTINFO_PATH = Path("/proc/self/mountinfo")

# Kernel and pseudo filesystems: they expose kernel or process state, never user data.
VIRTUAL_FS_TYPES = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "proc",
        "pstore",
        "rpc_pipefs",
        "securityfs",
        "selinuxfs",
        "sysfs",
        "tracefs",
    }
)

_OCTAL_ESCAPE = re.compile(rb"\\([0-7]{3})")


class MountInfoError(ValueError):
    """The mountinfo content could not be parsed."""


@dataclass(frozen=True, slots=True)
class MountEntry:
    """One line of mountinfo."""

    mount_id: int
    parent_id: int
    major_minor: str
    fs_root: str
    mount_point: str
    mount_options: frozenset[str]
    fs_type: str
    source: str
    super_options: frozenset[str]

    @property
    def is_read_only(self) -> bool:
        """True when this mount (not only the superblock) is read-only."""
        return "ro" in self.mount_options

    @property
    def is_virtual(self) -> bool:
        return self.fs_type in VIRTUAL_FS_TYPES


@dataclass(frozen=True, slots=True)
class MountTable:
    """All mounts visible to the process, in mountinfo order."""

    entries: tuple[MountEntry, ...]

    def mount_for(self, path: str) -> MountEntry | None:
        """Return the mount that holds ``path`` (an absolute, normalized path).

        The longest matching mount point wins. When several mounts share the same
        mount point, the last one listed is the visible one (it is stacked on top).
        """
        best: MountEntry | None = None
        best_len = -1
        for entry in self.entries:
            point = entry.mount_point
            if not _is_within(path, point):
                continue
            if len(point) >= best_len:
                best = entry
                best_len = len(point)
        return best


def _is_within(path: str, mount_point: str) -> bool:
    if mount_point == "/":
        return path.startswith("/")
    return path == mount_point or path.startswith(mount_point + "/")


def _unescape(field: bytes) -> str:
    raw = _OCTAL_ESCAPE.sub(lambda m: bytes([int(m.group(1), 8)]), field)
    return os.fsdecode(raw)


def parse_mountinfo(content: bytes) -> MountTable:
    """Parse the raw bytes of a mountinfo file."""
    entries: list[MountEntry] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split(b" ")
        try:
            separator = fields.index(b"-", 6)
        except ValueError as exc:
            raise MountInfoError(f"line {line_number}: missing '-' separator") from exc
        if separator + 3 > len(fields):
            raise MountInfoError(f"line {line_number}: truncated line")
        try:
            mount_id = int(fields[0])
            parent_id = int(fields[1])
        except ValueError as exc:
            raise MountInfoError(f"line {line_number}: invalid mount id") from exc
        entries.append(
            MountEntry(
                mount_id=mount_id,
                parent_id=parent_id,
                major_minor=fields[2].decode("ascii", errors="replace"),
                fs_root=_unescape(fields[3]),
                mount_point=_unescape(fields[4]),
                mount_options=frozenset(_unescape(fields[5]).split(",")),
                fs_type=_unescape(fields[separator + 1]),
                source=_unescape(fields[separator + 2]),
                super_options=frozenset(
                    _unescape(fields[separator + 3]).split(",")
                    if separator + 3 < len(fields)
                    else ()
                ),
            )
        )
    if not entries:
        raise MountInfoError("no mount entries found")
    return MountTable(entries=tuple(entries))


def read_mountinfo(path: Path = MOUNTINFO_PATH) -> MountTable:
    """Read and parse the mountinfo of the current process."""
    return parse_mountinfo(path.read_bytes())
