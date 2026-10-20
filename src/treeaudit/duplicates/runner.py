"""Running a duplicate analysis on a recorded scan (D-015, D-030).

Stages, each reading as little as possible, in the physical order of the files
on the disk (rotating disks pay 10-15 ms per seek):

1. digests already known from earlier analyses (unchanged files) are reused;
2. "repérage": the position of each file on the disk is asked (FIEMAP);
3. head: the first 4 KiB (the whole file if it is small), one read each;
4. tail: the last 4 KiB, only for files whose head still matches another;
5. full SHA-256, read sequentially, only for the remaining candidates;
6. optional byte-by-byte comparison.

Every file is opened through TreeReader (no symlink is ever followed) and must
still match the index before and after being read; otherwise it is reported and
left out of the groups. The run ends complete, incomplete, cancelled or failed.
"""

from __future__ import annotations

import functools
import logging
import os
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ScanLock, prepare_data_dir
from treeaudit.core.errors import TreeAuditError
from treeaudit.db import dupes, scans
from treeaudit.db.connection import connect, from_sqlite_int
from treeaudit.duplicates.hashing import (
    EDGE_SIZE,
    CancelledError,
    full_digest,
    head_digest,
    is_small,
    same_content,
    tail_digest,
)
from treeaudit.duplicates.placement import physical_offset, read_order_key
from treeaudit.duplicates.reader import (
    Expected,
    FileChangedError,
    TreeReader,
    check_unchanged,
)
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext, validate_root
from treeaudit.scanner.models import ScanStatus

logger = logging.getLogger(__name__)

DEFAULT_MIN_SIZE = 1024
MICRO_MAX_SIZE = DEFAULT_MIN_SIZE - 1


class DuplicateAnalysisError(TreeAuditError):
    """The analysis cannot start (unknown scan, root mismatch...)."""


@dataclass(frozen=True, slots=True)
class DupOptions:
    min_size: int = DEFAULT_MIN_SIZE
    max_size: int | None = None
    verify_bytes: bool = False
    micro: bool = False
    reuse_digests: bool = True
    """Reuse digests of earlier analyses for files whose inode, size and mtime did not change."""

    @classmethod
    def for_micro(cls, verify_bytes: bool = False) -> DupOptions:
        return cls(min_size=1, max_size=MICRO_MAX_SIZE, verify_bytes=verify_bytes, micro=True)


@dataclass(slots=True)
class DupProgress:
    """Written by the analysing thread only; other threads just read it."""

    run_id: int | None = None
    stage: str = ""
    candidates: int = 0
    files_hashed: int = 0
    """Distinct files read at least once (head, tail or full)."""
    reused: int = 0
    bytes_read: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class DupOutcome:
    run_id: int
    status: ScanStatus
    candidates: int
    files_hashed: int
    bytes_read: int
    errors: int
    failure: str | None = None
    reused: int = 0


def _expected(candidate: dupes.Candidate) -> Expected:
    return Expected(candidate.dev, candidate.ino, candidate.size, candidate.mtime_ns)


def _groups(
    items: Iterable[dupes.Candidate], key: Callable[[dupes.Candidate], object | None]
) -> list[list[dupes.Candidate]]:
    """Group candidates by (size, key) — skipping those without a key — keep groups of 2+."""
    buckets: dict[tuple[int, object], list[dupes.Candidate]] = defaultdict(list)
    for item in items:
        value = key(item)
        if value is not None:
            buckets[(item.size, value)].append(item)
    return [group for group in buckets.values() if len(group) >= 2]


class _Analysis:
    def __init__(
        self,
        reader: TreeReader,
        options: DupOptions,
        cancel: threading.Event,
        progress: DupProgress,
        known: dict[int, dupes.KnownDigests],
    ) -> None:
        self.reader = reader
        self.options = options
        self.cancel = cancel
        self.progress = progress
        self.head = {i: k.head for i, k in known.items() if k.head is not None}
        self.tail = {i: k.tail for i, k in known.items() if k.tail is not None}
        self.full = {i: k.full for i, k in known.items() if k.full is not None}
        self.known = set(known)
        self.read_ids: set[int] = set()
        self.order: dict[int, tuple[int, int, int]] = {}
        self.errors: list[dupes.DupError] = []

    def _read(self, data: int) -> None:
        self.progress.bytes_read += data

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise CancelledError

    def _error(self, candidate: dupes.Candidate, operation: str, exc: BaseException) -> None:
        errno = exc.errno if isinstance(exc, OSError) else None
        message = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else str(exc)
        self.errors.append(
            dupes.DupError(
                candidate.entry_id, candidate.rel_path_display, operation, errno, message
            )
        )
        self.progress.errors = len(self.errors)
        # Drop whatever was computed for a file that turned out to be unreliable.
        for table in (self.head, self.tail, self.full):
            table.pop(candidate.entry_id, None)

    def _with_file[R](
        self, candidate: dupes.Candidate, operation: str, work: Callable[[int], R]
    ) -> R | None:
        """Open the file safely, run work(fd), check it did not change, close it."""
        expected = _expected(candidate)
        try:
            fd = self.reader.open_file(candidate.rel_path, expected)
        except FileChangedError as exc:
            self._error(candidate, "changed", exc)
            return None
        except OSError as exc:
            self._error(candidate, "open", exc)
            return None
        try:
            result = work(fd)
            check_unchanged(fd, expected)
        except FileChangedError as exc:
            self._error(candidate, "changed", exc)
            return None
        except OSError as exc:
            self._error(candidate, operation, exc)
            return None
        finally:
            os.close(fd)
        return result

    def _ordered(self, items: Iterable[dupes.Candidate]) -> list[dupes.Candidate]:
        return sorted(
            items, key=lambda c: self.order.get(c.entry_id, read_order_key(None, c.dev, c.ino))
        )

    def _locate(self, items: list[dupes.Candidate]) -> None:
        """Ask the position of each file on the disk (in path order: directories are reused)."""
        for candidate in sorted(items, key=lambda c: c.rel_path):
            self._check_cancel()
            errors_before = len(self.errors)
            position = self._with_file(candidate, "locate", physical_offset)
            if len(self.errors) > errors_before:
                continue  # not readable: reported, and left out of every stage
            self.order[candidate.entry_id] = read_order_key(position, candidate.dev, candidate.ino)

    def _stage(
        self,
        name: str,
        items: list[dupes.Candidate],
        operation: str,
        work: Callable[[dupes.Candidate, int], bytes],
        store: dict[int, bytes],
    ) -> None:
        self.progress.stage = name
        for candidate in self._ordered(items):
            self._check_cancel()
            digest = self._with_file(candidate, operation, functools.partial(work, candidate))
            if digest is not None:
                store[candidate.entry_id] = digest
                self.read_ids.add(candidate.entry_id)
                self.progress.files_hashed = len(self.read_ids)

    def _head(self, candidate: dupes.Candidate, fd: int) -> bytes:
        digest, read = head_digest(fd, candidate.size)
        self._read(read)
        return digest

    def _tail(self, candidate: dupes.Candidate, fd: int) -> bytes:
        digest, read = tail_digest(fd, candidate.size)
        self._read(read)
        return digest

    def _full(self, candidate: dupes.Candidate, fd: int) -> bytes:
        return full_digest(fd, candidate.size, cancel=self.cancel, on_read=self._read)

    def _verify(self, group: list[dupes.Candidate]) -> None:
        reference = group[0]
        for other in group[1:]:
            self._check_cancel()
            try:
                fd_a = self.reader.open_file(reference.rel_path, _expected(reference))
                try:
                    fd_b = self.reader.open_file(other.rel_path, _expected(other))
                except BaseException:
                    os.close(fd_a)
                    raise
            except (OSError, FileChangedError) as exc:
                self._error(other, "verify", exc)
                continue
            try:
                identical = same_content(
                    fd_a, fd_b, other.size, cancel=self.cancel, on_read=self._read
                )
            except OSError as exc:
                identical = False
                self._error(other, "verify", exc)
            finally:
                os.close(fd_a)
                os.close(fd_b)
            if not identical and other.entry_id in self.full:
                self._error(
                    other, "verify", ValueError("same SHA-256 but different bytes (collision?)")
                )

    def run(self, candidates: list[dupes.Candidate]) -> None:
        # Every stage skips digests already known from an earlier analysis.
        need_head = [c for c in candidates if c.entry_id not in self.head]
        self.progress.stage = "repérage sur le disque"
        self._locate(need_head)
        need_head = [c for c in need_head if c.entry_id in self.order]
        self._stage("début des fichiers", need_head, "read", self._head, self.head)
        for candidate in candidates:
            if is_small(candidate.size) and candidate.entry_id in self.head:
                self.full.setdefault(candidate.entry_id, self.head[candidate.entry_id])

        large = [c for c in candidates if not is_small(c.size)]
        same_head = [c for g in _groups(large, lambda c: self.head.get(c.entry_id)) for c in g]
        need_tail = [c for c in same_head if c.entry_id not in self.tail]
        self._stage("fin des fichiers", need_tail, "read", self._tail, self.tail)

        def head_and_tail(c: dupes.Candidate) -> object | None:
            if c.entry_id not in self.head or c.entry_id not in self.tail:
                return None
            return (self.head[c.entry_id], self.tail[c.entry_id])

        same_edges = [c for g in _groups(same_head, head_and_tail) for c in g]
        need_full = [c for c in same_edges if c.entry_id not in self.full]
        self._stage("lecture complète", need_full, "read", self._full, self.full)

        if self.options.verify_bytes:
            self.progress.stage = "comparaison octet par octet"
            for group in _groups(candidates, lambda c: self.full.get(c.entry_id)):
                self._verify(group)

        self.progress.reused = sum(
            1 for c in candidates if c.entry_id in self.known and c.entry_id not in self.read_ids
        )

    def hash_rows(self, candidates: list[dupes.Candidate]) -> list[dupes.HashRow]:
        return [
            dupes.HashRow(
                c.entry_id,
                c.size,
                self.head.get(c.entry_id),
                self.full.get(c.entry_id),
                self.tail.get(c.entry_id),
            )
            for c in candidates
            if c.entry_id in self.head or c.entry_id in self.full
        ]


def _expected_root(conn: sqlite3.Connection, scan: sqlite3.Row, root_path: str) -> tuple[int, int]:
    """Identity the root must still have: the one recorded by the scan when known."""
    if not scan["subpath"]:
        start = conn.execute(
            "SELECT dev, ino FROM entry WHERE scan_id = ? AND depth = 0", (scan["id"],)
        ).fetchone()
        if start is not None:
            return from_sqlite_int(start["dev"]), from_sqlite_int(start["ino"])
    # Sub-folder scan: the root itself was not recorded; every file is still
    # checked against the index before it is read.
    st = os.lstat(root_path)
    return st.st_dev, st.st_ino


def run_duplicates(
    conn: sqlite3.Connection,
    scan_id: int,
    root: RootConfig,
    *,
    ctx: ValidationContext,
    options: DupOptions,
    cancel: threading.Event,
    progress: DupProgress | None = None,
) -> DupOutcome:
    scan = scans.get_scan(conn, scan_id)
    if scan is None:
        raise DuplicateAnalysisError(f"unknown scan: {scan_id}")
    if scan["root_id"] != root.id or scan["root_path"] != root.path:
        raise DuplicateAnalysisError(f"scan {scan_id} was made on another root ({scan['root_id']})")
    validate_root(root.path, ctx, allow_system_volume=root.allow_system_volume)
    expected_root = _expected_root(conn, scan, root.path)

    progress = progress if progress is not None else DupProgress()
    candidates = dupes.load_candidates(
        conn, scan_id, min_size=options.min_size, max_size=options.max_size
    )
    run_id = dupes.create_run(
        conn,
        scan_id,
        {
            "min_size": options.min_size,
            "max_size": options.max_size,
            "verify_bytes": options.verify_bytes,
            "micro": options.micro,
            "reuse_digests": options.reuse_digests,
            "edge_size": EDGE_SIZE,
        },
    )
    progress.run_id = run_id
    progress.candidates = len(candidates)
    known = (
        dupes.known_digests(conn, candidates, edge_size=EDGE_SIZE) if options.reuse_digests else {}
    )

    status = ScanStatus.COMPLETE
    failure: str | None = None
    analysis: _Analysis | None = None
    try:
        with TreeReader(root.path, expected_root) as reader:
            analysis = _Analysis(reader, options, cancel, progress, known)
            try:
                analysis.run(candidates)
            except CancelledError:
                status = ScanStatus.CANCELLED
        if status is ScanStatus.COMPLETE and analysis.errors:
            status = ScanStatus.INCOMPLETE
        dupes.save_results(conn, run_id, analysis.hash_rows(candidates), analysis.errors)
    except Exception as exc:
        logger.exception("duplicate analysis %d failed", run_id)
        status = ScanStatus.FAILED
        failure = f"{type(exc).__name__}: {exc}"

    errors = len(analysis.errors) if analysis is not None else 0
    dupes.finish_run(
        conn,
        run_id,
        status=status,
        candidates=len(candidates),
        files_hashed=progress.files_hashed,
        bytes_read=progress.bytes_read,
        errors=errors,
        reused=progress.reused,
        failure=failure,
    )
    return DupOutcome(
        run_id,
        status,
        len(candidates),
        progress.files_hashed,
        progress.bytes_read,
        errors,
        failure,
        progress.reused,
    )


def execute_duplicates(
    config: AppConfig,
    scan_id: int,
    options: DupOptions,
    *,
    mounts: MountTable,
    home: str | None,
    cancel: threading.Event,
    progress: DupProgress | None = None,
) -> DupOutcome:
    """Take the lock (one heavy job at a time) and run the analysis."""
    prepare_data_dir(config.data_dir)
    ctx = ValidationContext(
        mounts=mounts,
        home=home,
        data_dir=config.data_dir,
        check_filesystem=True,
        require_read_only_mount=config.container_mode,
    )
    with ScanLock(config.data_dir):
        conn = connect(config.data_dir)
        try:
            scans.fail_interrupted_scans(conn)
            dupes.fail_interrupted_runs(conn)
            scan = scans.get_scan(conn, scan_id)
            if scan is None:
                raise DuplicateAnalysisError(f"unknown scan: {scan_id}")
            root = config.get_root(scan["root_id"])
            if root is None:
                raise DuplicateAnalysisError(
                    f"root {scan['root_id']!r} of scan {scan_id} is not configured anymore"
                )
            return run_duplicates(
                conn, scan_id, root, ctx=ctx, options=options, cancel=cancel, progress=progress
            )
        finally:
            conn.close()
