"""Records produced by the walker and statuses of a scan (D-012, D-014, D-022)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ScanStatus(StrEnum):
    """Where a scan stands (D-012). It ends in one of the four final statuses:
    ``complete`` only when no error was met; ``incomplete`` when some folder or
    entry could not be read (excluded entries do not count); ``cancelled`` when
    stopped by the user; ``failed`` when the scan itself broke. The values are
    stored in the index: never rename one."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_final(self) -> bool:
        """The scan has ended and will not change any more."""
        return self not in (ScanStatus.PENDING, ScanStatus.RUNNING)


class EntryKind(StrEnum):
    """What an entry is, from lstat: a symbolic link is never followed (D-013)."""

    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    OTHER = "other"  # fifo, socket, device: listed, never opened


class ExclusionReason(StrEnum):
    """Why an entry was deliberately left out. An exclusion is a choice, not a
    failure: it never makes a scan incomplete."""

    MOUNT_POINT = "mount_point"
    TREEAUDIT = "treeaudit"  # TreeAudit's own data or archive directory (D-037)
    PRIVATE = "private"  # secrets of the personal folder, never read (D-037)


class ErrorOperation(StrEnum):
    """The step that failed, stored with each error. Any error makes the scan
    incomplete (D-012)."""

    OPEN_ROOT = "open_root"
    OPEN_SUBPATH = "open_subpath"
    OPEN_DIR = "open_dir"
    LIST_DIR = "list_dir"
    STAT_ENTRY = "stat_entry"
    READ_LINK = "read_link"
    CHANGED_DURING_SCAN = "changed_during_scan"
    MAX_DEPTH = "max_depth"


@dataclass(frozen=True, slots=True)
class EntryRecord:
    """One entry as lstat saw it, ready to be stored. Names and paths are bytes,
    exactly as on disk (D-014); ``rel_path`` is relative to the root."""

    entry_id: int
    parent_id: int | None
    depth: int
    name: bytes
    rel_path: bytes
    kind: EntryKind
    size: int
    allocated_size: int
    mtime_ns: int
    mode: int
    uid: int
    gid: int
    dev: int
    ino: int
    nlink: int
    link_target: bytes | None = None


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """A path that could not be read, and why. ``message`` comes from the
    system (strerror), never from the content of a file."""

    rel_path: bytes
    operation: ErrorOperation
    errno: int | None
    message: str


@dataclass(frozen=True, slots=True)
class ExclusionRecord:
    """A path left out on purpose (mount point, TreeAudit's own folder, secret)."""

    rel_path: bytes
    reason: ExclusionReason


@dataclass(slots=True)
class Totals:
    """Aggregates of a subtree; each inode is counted once (D-022)."""

    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    others: int = 0
    logical_size: int = 0
    allocated_size: int = 0
    errors: int = 0

    def add(self, other: Totals) -> None:
        """Add a child's totals to its parent's, when the walk leaves the child."""
        self.files += other.files
        self.dirs += other.dirs
        self.symlinks += other.symlinks
        self.others += other.others
        self.logical_size += other.logical_size
        self.allocated_size += other.allocated_size
        self.errors += other.errors


@dataclass(frozen=True, slots=True)
class DirDone:
    """Emitted when the walker leaves a directory, with its subtree totals."""

    entry_id: int
    totals: Totals = field(default_factory=Totals)


WalkEvent = EntryRecord | ErrorRecord | ExclusionRecord | DirDone
