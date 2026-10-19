"""Reading and writing scans in the index."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from treeaudit.db.connection import to_sqlite_int
from treeaudit.scanner.models import (
    EntryKind,
    EntryRecord,
    ErrorRecord,
    ExclusionRecord,
    ScanStatus,
    Totals,
)

DEFAULT_BATCH_SIZE = 2000
_ENTRY_COLUMNS = 21
# Only constant '?' placeholders are interpolated: no data reaches the SQL text.
_INSERT_ENTRY = f"INSERT INTO entry VALUES ({', '.join('?' * _ENTRY_COLUMNS)})"  # noqa: S608
MAX_EXTENSION_LENGTH = 32


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def display(raw: bytes) -> tuple[str, bool]:
    """Printable form of a raw name; invalid UTF-8 bytes appear as \\xNN escapes."""
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="backslashreplace"), False


def extension_of(name: bytes) -> str | None:
    """Lower-cased extension of a file name, or None ('.bashrc' has none)."""
    stem, dot, suffix = name.rpartition(b".")
    if not dot or not stem or not suffix or len(suffix) > MAX_EXTENSION_LENGTH:
        return None
    text, _ = display(suffix)
    return text.lower()


def create_scan(
    conn: sqlite3.Connection, *, root_id: str, root_path: str, subpath: str, options: dict[str, Any]
) -> int:
    cursor = conn.execute(
        "INSERT INTO scan (root_id, root_path, subpath, status, started_at, options) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (root_id, root_path, subpath, ScanStatus.RUNNING.value, now_iso(), json.dumps(options)),
    )
    scan_id = cursor.lastrowid
    if scan_id is None:
        raise RuntimeError("SQLite did not return the new scan id")
    return scan_id


def finish_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    status: ScanStatus,
    entries: int,
    totals: Totals,
    errors: int,
    exclusions: int,
    failure: str | None = None,
) -> None:
    if not status.is_final:
        raise ValueError(f"not a final status: {status}")
    conn.execute(
        "UPDATE scan SET status = ?, finished_at = ?, entries = ?, files = ?, dirs = ?, "
        "symlinks = ?, others = ?, logical_size = ?, allocated_size = ?, errors = ?, "
        "exclusions = ?, failure = ? WHERE id = ?",
        (
            status.value,
            now_iso(),
            entries,
            totals.files,
            totals.dirs,
            totals.symlinks,
            totals.others,
            totals.logical_size,
            totals.allocated_size,
            errors,
            exclusions,
            failure,
            scan_id,
        ),
    )


def fail_interrupted_scans(conn: sqlite3.Connection) -> int:
    """Mark scans left 'running' or 'pending' by a crash as failed (D-012).

    Only call this while holding the scan lock, so no live scan is affected.
    """
    cursor = conn.execute(
        "UPDATE scan SET status = ?, finished_at = ?, failure = ? WHERE status IN (?, ?)",
        (
            ScanStatus.FAILED.value,
            now_iso(),
            "interrupted: the process stopped before the scan finished",
            ScanStatus.RUNNING.value,
            ScanStatus.PENDING.value,
        ),
    )
    return cursor.rowcount


class ScanWriter:
    """Buffers walker events and writes them in transactions of ``batch_size`` rows."""

    def __init__(
        self, conn: sqlite3.Connection, scan_id: int, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        self.conn = conn
        self.scan_id = scan_id
        self.batch_size = batch_size
        self._entries: list[tuple[Any, ...]] = []
        self._summaries: list[tuple[Any, ...]] = []
        self._errors: list[tuple[Any, ...]] = []
        self._exclusions: list[tuple[Any, ...]] = []

    def _pending(self) -> int:
        return len(self._entries) + len(self._summaries) + len(self._errors) + len(self._exclusions)

    def _maybe_flush(self) -> None:
        if self._pending() >= self.batch_size:
            self.flush()

    def add_entry(self, record: EntryRecord) -> None:
        name_display, name_valid = display(record.name)
        rel_display, _ = display(record.rel_path)
        extension = extension_of(record.name) if record.kind is EntryKind.FILE else None
        self._entries.append(
            (
                self.scan_id,
                record.entry_id,
                record.parent_id,
                record.depth,
                record.name,
                name_display,
                int(name_valid),
                record.rel_path,
                rel_display,
                record.kind.value,
                extension,
                record.size,
                record.allocated_size,
                record.mtime_ns,
                record.mode,
                record.uid,
                record.gid,
                to_sqlite_int(record.dev),
                to_sqlite_int(record.ino),
                record.nlink,
                record.link_target,
            )
        )
        self._maybe_flush()

    def add_dir_summary(self, entry_id: int, totals: Totals) -> None:
        self._summaries.append(
            (
                self.scan_id,
                entry_id,
                totals.files,
                totals.dirs,
                totals.symlinks,
                totals.others,
                totals.logical_size,
                totals.allocated_size,
                totals.errors,
            )
        )
        self._maybe_flush()

    def add_error(self, record: ErrorRecord) -> None:
        rel_display, _ = display(record.rel_path)
        self._errors.append(
            (
                self.scan_id,
                record.rel_path,
                rel_display,
                record.operation.value,
                record.errno,
                record.message,
            )
        )
        self._maybe_flush()

    def add_exclusion(self, record: ExclusionRecord) -> None:
        rel_display, _ = display(record.rel_path)
        self._exclusions.append((self.scan_id, record.rel_path, rel_display, record.reason.value))
        self._maybe_flush()

    def flush(self) -> None:
        if not self._pending():
            return
        conn = self.conn
        conn.execute("BEGIN")
        try:
            conn.executemany(
                _INSERT_ENTRY,
                self._entries,
            )
            # Summaries reference entries: insert them after the entries of the batch.
            conn.executemany(
                "INSERT INTO dir_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self._summaries
            )
            conn.executemany(
                "INSERT INTO scan_error (scan_id, rel_path, rel_path_display, operation, errno, "
                "message) VALUES (?, ?, ?, ?, ?, ?)",
                self._errors,
            )
            conn.executemany(
                "INSERT INTO scan_exclusion (scan_id, rel_path, rel_path_display, reason) "
                "VALUES (?, ?, ?, ?)",
                self._exclusions,
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        self._entries.clear()
        self._summaries.clear()
        self._errors.clear()
        self._exclusions.clear()


# --- queries ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtensionStat:
    extension: str | None
    files: int
    logical_size: int


@dataclass(frozen=True, slots=True)
class DirStat:
    rel_path_display: str
    depth: int
    files: int
    dirs: int
    allocated_size: int
    errors: int


def get_scan(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM scan WHERE id = ?", (scan_id,)).fetchone()
    return row


def list_scans(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM scan ORDER BY id DESC LIMIT ?", (limit,)))


def top_extensions(conn: sqlite3.Connection, scan_id: int, limit: int = 15) -> list[ExtensionStat]:
    rows = conn.execute(
        "SELECT extension, COUNT(*) AS files, SUM(size) AS logical_size FROM entry "
        "WHERE scan_id = ? AND kind = 'file' GROUP BY extension "
        "ORDER BY logical_size DESC, files DESC LIMIT ?",
        (scan_id, limit),
    )
    return [ExtensionStat(r["extension"], r["files"], r["logical_size"] or 0) for r in rows]


def largest_dirs(conn: sqlite3.Connection, scan_id: int, limit: int = 15) -> list[DirStat]:
    rows = conn.execute(
        "SELECT e.rel_path_display, e.depth, d.files, d.dirs, d.allocated_size, d.errors "
        "FROM dir_summary d JOIN entry e USING (scan_id, entry_id) "
        "WHERE d.scan_id = ? AND e.depth > 0 "
        "ORDER BY d.allocated_size DESC LIMIT ?",
        (scan_id, limit),
    )
    return [
        DirStat(
            r["rel_path_display"],
            r["depth"],
            r["files"],
            r["dirs"],
            r["allocated_size"],
            r["errors"],
        )
        for r in rows
    ]


def list_errors(conn: sqlite3.Connection, scan_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT rel_path_display, operation, errno, message FROM scan_error "
            "WHERE scan_id = ? ORDER BY id LIMIT ?",
            (scan_id, limit),
        )
    )


def list_exclusions(conn: sqlite3.Connection, scan_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT rel_path_display, reason FROM scan_exclusion "
            "WHERE scan_id = ? ORDER BY id LIMIT ?",
            (scan_id, limit),
        )
    )
