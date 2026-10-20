"""Running a duplicate analysis on a recorded scan (D-015).

size (index only) -> partial digest -> full SHA-256 -> optional byte comparison.
Every file is opened through TreeReader (no symlink is ever followed) and must
still match the index before and after being read; otherwise it is reported and
left out of the groups. The run ends complete, incomplete, cancelled or failed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ScanLock, prepare_data_dir
from treeaudit.core.errors import TreeAuditError
from treeaudit.db import dupes, scans
from treeaudit.db.connection import connect, from_sqlite_int
from treeaudit.duplicates.hashing import (
    CancelledError,
    full_digest,
    is_small,
    partial_digest,
    same_content,
)
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


def _expected(candidate: dupes.Candidate) -> Expected:
    return Expected(candidate.dev, candidate.ino, candidate.size, candidate.mtime_ns)


def _groups[K](items: Iterable[dupes.Candidate], key: dict[int, K]) -> list[list[dupes.Candidate]]:
    """Group candidates by key (only those with a key), keep groups of 2 or more."""
    buckets: dict[tuple[int, K], list[dupes.Candidate]] = defaultdict(list)
    for item in items:
        if item.entry_id in key:
            buckets[(item.size, key[item.entry_id])].append(item)
    return [group for group in buckets.values() if len(group) >= 2]


class _Analysis:
    def __init__(
        self,
        reader: TreeReader,
        options: DupOptions,
        cancel: threading.Event,
        progress: DupProgress,
    ) -> None:
        self.reader = reader
        self.options = options
        self.cancel = cancel
        self.progress = progress
        self.partial: dict[int, bytes] = {}
        self.full: dict[int, bytes] = {}
        self.errors: list[dupes.DupError] = []

    def _read(self, data: int) -> None:
        self.progress.bytes_read += data

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
        self.partial.pop(candidate.entry_id, None)
        self.full.pop(candidate.entry_id, None)

    def _hash(self, candidate: dupes.Candidate, *, full: bool) -> None:
        expected = _expected(candidate)
        try:
            fd = self.reader.open_file(candidate.rel_path, expected)
        except FileChangedError as exc:
            self._error(candidate, "changed", exc)
            return
        except OSError as exc:
            self._error(candidate, "open", exc)
            return
        try:
            if full:
                digest = full_digest(fd, candidate.size, cancel=self.cancel, on_read=self._read)
            else:
                digest, read = partial_digest(fd, candidate.size)
                self._read(read)
            check_unchanged(fd, expected)
        except FileChangedError as exc:
            self._error(candidate, "changed", exc)
            return
        except OSError as exc:
            self._error(candidate, "read", exc)
            return
        finally:
            os.close(fd)
        if full:
            self.full[candidate.entry_id] = digest
        else:
            self.partial[candidate.entry_id] = digest
            if is_small(candidate.size):  # the partial digest covered everything
                self.full[candidate.entry_id] = digest
        self.progress.files_hashed += 1

    def _verify(self, group: list[dupes.Candidate]) -> None:
        reference = group[0]
        for other in group[1:]:
            if self.cancel.is_set():
                raise CancelledError
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
        self.progress.stage = "empreintes partielles"
        for group in _groups(candidates, {c.entry_id: None for c in candidates}):
            for candidate in group:
                if self.cancel.is_set():
                    raise CancelledError
                self._hash(candidate, full=False)

        self.progress.stage = "empreintes complètes"
        for group in _groups(candidates, self.partial):
            for candidate in group:
                if candidate.entry_id in self.full:
                    continue
                if self.cancel.is_set():
                    raise CancelledError
                self._hash(candidate, full=True)

        if self.options.verify_bytes:
            self.progress.stage = "comparaison octet par octet"
            for group in _groups(candidates, self.full):
                self._verify(group)

    def hash_rows(self, candidates: list[dupes.Candidate]) -> list[dupes.HashRow]:
        return [
            dupes.HashRow(
                c.entry_id,
                c.size,
                self.partial.get(c.entry_id),
                self.full.get(c.entry_id),
            )
            for c in candidates
            if c.entry_id in self.partial or c.entry_id in self.full
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
        },
    )
    progress.run_id = run_id
    progress.candidates = len(candidates)

    status = ScanStatus.COMPLETE
    failure: str | None = None
    analysis: _Analysis | None = None
    try:
        with TreeReader(root.path, expected_root) as reader:
            analysis = _Analysis(reader, options, cancel, progress)
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
