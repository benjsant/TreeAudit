"""Iterative, descriptor-based directory walk (D-013, D-021, D-022).

Every directory is opened relative to its parent's file descriptor with
O_NOFOLLOW, then checked with fstat against what the listing reported. A
directory swapped for a symlink (or for another directory) between the listing
and the descent is reported as an error instead of being followed, so the walk
cannot leave the root. File contents are never read.

The walk is a generator of events; the consumer may stop it at any time
(``close()``), which releases every open descriptor. At most one descriptor per
depth level is open, plus one for the directory being listed.
"""

from __future__ import annotations

import errno as errno_codes
import itertools
import os
import stat
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field

from treeaudit.scanner.models import (
    DirDone,
    EntryKind,
    EntryRecord,
    ErrorOperation,
    ErrorRecord,
    ExclusionReason,
    ExclusionRecord,
    Totals,
    WalkEvent,
)

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Avoid updating access times when allowed (only for files the user owns).
_NOATIME = getattr(os, "O_NOATIME", 0)

DEFAULT_MAX_DEPTH = 256

# Entries of a directory are examined by batches sorted by inode number: on ext4
# (inode tables) and NTFS (MFT) neighbouring inodes are stored close together, so
# a rotating disk seeks far less than in directory order. Batches bound memory
# for huge directories.
STAT_BATCH = 50_000


class SubpathError(ValueError):
    """The requested sub-folder is not a safe relative path."""


@dataclass(frozen=True, slots=True)
class WalkOptions:
    root_path: str
    """Absolute path of the root as seen by this process (validated beforehand)."""
    mount_points: frozenset[str] = frozenset()
    """Absolute mount points strictly below the root, read from mountinfo."""
    cross_mounts: bool = False
    max_depth: int = DEFAULT_MAX_DEPTH
    expected_root: tuple[int, int] | None = None
    """(st_dev, st_ino) of the root at validation time, to detect a swap."""
    own_dirs: frozenset[tuple[int, int]] = frozenset()
    """(st_dev, st_ino) of TreeAudit's own directories (data, archives): never walked.
    Recognised by identity, not by name, so that a renamed or moved folder still is."""
    private_paths: frozenset[bytes] = frozenset()
    """Paths relative to the root that are never even examined (secrets, D-037)."""


def parse_subpath(subpath: str) -> tuple[str, ...]:
    """Split a sub-folder given relative to the root, refusing anything unsafe."""
    if subpath in ("", "."):
        return ()
    if "\x00" in subpath:
        raise SubpathError("sub-folder contains a NUL character")
    if subpath.startswith("/"):
        raise SubpathError("sub-folder must be relative to the root")
    parts = tuple(subpath.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise SubpathError("sub-folder must not contain empty, '.' or '..' components")
    return parts


def _open_dir(name: str, dir_fd: int | None) -> int:
    """Open a directory without following symlinks.

    Kept as a separate function so tests can simulate a directory being swapped.
    """
    if _NOATIME:
        try:
            return os.open(name, DIR_FLAGS | _NOATIME, dir_fd=dir_fd)
        except PermissionError as exc:
            # EPERM: O_NOATIME refused (not the owner). EACCES is a real denial.
            if exc.errno != errno_codes.EPERM:
                raise
    return os.open(name, DIR_FLAGS, dir_fd=dir_fd)


def _lstat_at(name: str, dir_fd: int) -> os.stat_result:
    """lstat of a listed entry, relative to its directory.

    Separate function so tests can simulate races.
    """
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _kind_of(mode: int) -> EntryKind:
    if stat.S_ISREG(mode):
        return EntryKind.FILE
    if stat.S_ISDIR(mode):
        return EntryKind.DIR
    if stat.S_ISLNK(mode):
        return EntryKind.SYMLINK
    return EntryKind.OTHER


def _error(rel_path: bytes, operation: ErrorOperation, exc: OSError) -> ErrorRecord:
    return ErrorRecord(
        rel_path=rel_path,
        operation=operation,
        errno=exc.errno,
        message=exc.strerror or type(exc).__name__,
    )


def _join(parent: bytes, name: bytes) -> bytes:
    return name if not parent else parent + b"/" + name


@dataclass(slots=True)
class _Frame:
    fd: int
    entry_id: int
    rel_path: bytes
    depth: int
    totals: Totals = field(default_factory=Totals)
    # (entry_id, name, rel_path, st_dev, st_ino) of subdirectories still to visit.
    subdirs: list[tuple[int, str, bytes, int, int]] = field(default_factory=list)
    next_subdir: int = 0


class _Walk:
    def __init__(self, options: WalkOptions) -> None:
        self.options = options
        self.ids = itertools.count(1)
        self.seen_inodes: set[tuple[int, int]] = set()

    def _count(self, totals: Totals, kind: EntryKind, st: os.stat_result) -> None:
        if kind is EntryKind.FILE:
            totals.files += 1
        elif kind is EntryKind.DIR:
            totals.dirs += 1
        elif kind is EntryKind.SYMLINK:
            totals.symlinks += 1
        else:
            totals.others += 1
        key = (st.st_dev, st.st_ino)
        if key in self.seen_inodes:
            return  # hard link already counted (D-022)
        if st.st_nlink > 1 and kind is not EntryKind.DIR:
            self.seen_inodes.add(key)
        totals.allocated_size += st.st_blocks * 512
        if kind is EntryKind.FILE:
            totals.logical_size += st.st_size

    def _record(
        self,
        *,
        entry_id: int,
        parent_id: int | None,
        depth: int,
        name: bytes,
        rel_path: bytes,
        st: os.stat_result,
        link_target: bytes | None = None,
    ) -> EntryRecord:
        return EntryRecord(
            entry_id=entry_id,
            parent_id=parent_id,
            depth=depth,
            name=name,
            rel_path=rel_path,
            kind=_kind_of(st.st_mode),
            size=st.st_size,
            allocated_size=st.st_blocks * 512,
            mtime_ns=st.st_mtime_ns,
            mode=st.st_mode,
            uid=st.st_uid,
            gid=st.st_gid,
            dev=st.st_dev,
            ino=st.st_ino,
            nlink=st.st_nlink,
            link_target=link_target,
        )

    def _list(self, frame: _Frame) -> Iterator[WalkEvent]:
        """List one directory: emit its entries and remember its subdirectories."""
        try:
            with os.scandir(frame.fd) as listing:
                batch: list[tuple[int, str]] = []
                for entry in listing:
                    batch.append((entry.inode(), entry.name))  # d_ino: no extra syscall
                    if len(batch) >= STAT_BATCH:
                        yield from self._stat_batch(frame, batch)
                        batch = []
                yield from self._stat_batch(frame, batch)
        except OSError as exc:
            frame.totals.errors += 1
            yield _error(frame.rel_path, ErrorOperation.LIST_DIR, exc)

    def _stat_batch(self, frame: _Frame, batch: list[tuple[int, str]]) -> Iterator[WalkEvent]:
        batch.sort()
        for _, entry_name in batch:
            yield from self._entry(frame, entry_name)

    def _entry(self, frame: _Frame, entry_name: str) -> Iterator[WalkEvent]:
        name = os.fsencode(entry_name)
        rel_path = _join(frame.rel_path, name)
        if rel_path in self.options.private_paths:
            yield ExclusionRecord(rel_path, ExclusionReason.PRIVATE)
            return
        try:
            st = _lstat_at(entry_name, frame.fd)
        except OSError as exc:
            frame.totals.errors += 1
            yield _error(rel_path, ErrorOperation.STAT_ENTRY, exc)
            return
        kind = _kind_of(st.st_mode)
        if kind is EntryKind.DIR and (st.st_dev, st.st_ino) in self.options.own_dirs:
            yield ExclusionRecord(rel_path, ExclusionReason.TREEAUDIT)
            return
        link_target: bytes | None = None
        if kind is EntryKind.SYMLINK:
            try:
                link_target = os.fsencode(os.readlink(entry_name, dir_fd=frame.fd))
            except OSError as exc:
                frame.totals.errors += 1
                yield _error(rel_path, ErrorOperation.READ_LINK, exc)
        entry_id = next(self.ids)
        self._count(frame.totals, kind, st)
        yield self._record(
            entry_id=entry_id,
            parent_id=frame.entry_id,
            depth=frame.depth + 1,
            name=name,
            rel_path=rel_path,
            st=st,
            link_target=link_target,
        )
        if kind is EntryKind.DIR:
            frame.subdirs.append((entry_id, entry_name, rel_path, st.st_dev, st.st_ino))

    def _open_start(self, subpath: tuple[str, ...]) -> Iterator[WalkEvent | int]:
        """Open the root, then the sub-folder component by component.

        Yields error events, and finally the descriptor of the start directory.
        """
        try:
            fd = _open_dir(self.options.root_path, None)
        except OSError as exc:
            yield _error(b"", ErrorOperation.OPEN_ROOT, exc)
            return
        if self.options.expected_root is not None:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != self.options.expected_root:
                os.close(fd)
                yield ErrorRecord(
                    b"", ErrorOperation.CHANGED_DURING_SCAN, None, "root changed since validation"
                )
                return
        rel_path = b""
        for part in subpath:
            rel_path = _join(rel_path, os.fsencode(part))
            if rel_path in self.options.private_paths:
                os.close(fd)
                yield ErrorRecord(
                    rel_path, ErrorOperation.OPEN_SUBPATH, None, "private folder, never read"
                )
                return
            try:
                child = _open_dir(part, fd)
            except OSError as exc:
                os.close(fd)
                yield _error(rel_path, ErrorOperation.OPEN_SUBPATH, exc)
                return
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) in self.options.own_dirs:
                os.close(fd)
                yield ErrorRecord(
                    rel_path, ErrorOperation.OPEN_SUBPATH, None, "TreeAudit's own folder"
                )
                return
        yield fd

    def run(self, subpath: tuple[str, ...]) -> Generator[WalkEvent, None, None]:
        start_fd: int | None = None
        for item in self._open_start(subpath):
            if isinstance(item, int):
                start_fd = item
            else:
                yield item
        if start_fd is None:
            return

        stack: list[_Frame] = []
        try:
            st = os.fstat(start_fd)
            name = os.fsencode(subpath[-1] if subpath else os.path.basename(self.options.root_path))
            rel_path = os.fsencode("/".join(subpath))
            start = _Frame(fd=start_fd, entry_id=next(self.ids), rel_path=rel_path, depth=0)
            start_fd = None  # now owned by the stack
            stack.append(start)
            start.totals.allocated_size += st.st_blocks * 512
            yield self._record(
                entry_id=start.entry_id,
                parent_id=None,
                depth=0,
                name=name,
                rel_path=rel_path,
                st=st,
            )
            yield from self._list(start)
            yield from self._descend(stack)
        finally:
            if start_fd is not None:
                os.close(start_fd)
            for frame in stack:
                os.close(frame.fd)

    def _descend(self, stack: list[_Frame]) -> Iterator[WalkEvent]:
        options = self.options
        while stack:
            frame = stack[-1]
            if frame.next_subdir >= len(frame.subdirs):
                stack.pop()
                os.close(frame.fd)
                yield DirDone(entry_id=frame.entry_id, totals=frame.totals)
                if stack:
                    stack[-1].totals.add(frame.totals)
                continue

            entry_id, name, rel_path, dev, ino = frame.subdirs[frame.next_subdir]
            frame.next_subdir += 1

            absolute = options.root_path.rstrip("/") + "/" + os.fsdecode(rel_path)
            if absolute in options.mount_points and not options.cross_mounts:
                yield ExclusionRecord(rel_path, ExclusionReason.MOUNT_POINT)
                continue
            if frame.depth + 1 > options.max_depth:
                frame.totals.errors += 1
                yield ErrorRecord(
                    rel_path,
                    ErrorOperation.MAX_DEPTH,
                    None,
                    f"maximum depth {options.max_depth} reached",
                )
                continue
            try:
                fd = _open_dir(name, frame.fd)
            except OSError as exc:
                frame.totals.errors += 1
                yield _error(rel_path, ErrorOperation.OPEN_DIR, exc)
                continue
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != (dev, ino):
                os.close(fd)
                frame.totals.errors += 1
                yield ErrorRecord(
                    rel_path,
                    ErrorOperation.CHANGED_DURING_SCAN,
                    None,
                    "directory replaced between listing and opening",
                )
                continue
            child = _Frame(fd=fd, entry_id=entry_id, rel_path=rel_path, depth=frame.depth + 1)
            stack.append(child)
            yield from self._list(child)


def walk(options: WalkOptions, subpath: tuple[str, ...] = ()) -> Generator[WalkEvent, None, None]:
    """Walk the root (or one of its sub-folders) and yield events in order.

    Parents are always emitted before their children. The start directory is the
    first entry (depth 0); its DirDone event, emitted last, carries the totals.
    """
    return _Walk(options).run(subpath)
