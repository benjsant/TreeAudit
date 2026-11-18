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

DEFAULT_BATCH_SIZE = 10_000  # measured: larger transactions, -5 % scan time
_ENTRY_COLUMNS = 21
# Only constant '?' placeholders are interpolated: no data reaches the SQL text.
_INSERT_ENTRY = f"INSERT INTO entry VALUES ({', '.join('?' * _ENTRY_COLUMNS)})"  # noqa: S608
MAX_EXTENSION_LENGTH = 32


def now_iso() -> str:
    """The current time in UTC, to the second, as stored in the index."""
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
    """Record a new scan as running and return its id."""
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
    """Record how a scan ended, with its totals; only a final status is
    accepted (D-012)."""
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
        """Queue one entry. The raw name is stored as bytes (D-014), next to a
        printable form and whether that form is exact (valid UTF-8)."""
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
        """Queue the totals of a folder's whole subtree (sent when the walk leaves it)."""
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
        """Queue a read error."""
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
        """Queue a path left out on purpose."""
        rel_display, _ = display(record.rel_path)
        self._exclusions.append((self.scan_id, record.rel_path, rel_display, record.reason.value))
        self._maybe_flush()

    def flush(self) -> None:
        """Write everything queued in one transaction. The runner calls it
        once more when the walk ends or is cancelled; a scan that fails keeps
        only the batches written before the failure."""
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
    """Files of one extension (None: no extension) and their apparent size."""

    extension: str | None
    files: int
    logical_size: int


@dataclass(frozen=True, slots=True)
class DirStat:
    """A folder and the totals of its whole subtree."""

    rel_path_display: str
    depth: int
    files: int
    dirs: int
    allocated_size: int
    errors: int


def get_scan(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row | None:
    """One scan by id, whatever its state; None if unknown."""
    row: sqlite3.Row | None = conn.execute("SELECT * FROM scan WHERE id = ?", (scan_id,)).fetchone()
    return row


def list_scans(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """The most recent scans first."""
    return list(conn.execute("SELECT * FROM scan ORDER BY id DESC LIMIT ?", (limit,)))


def top_extensions(conn: sqlite3.Connection, scan_id: int, limit: int = 15) -> list[ExtensionStat]:
    """The extensions that weigh the most, by apparent size. As in the totals
    (D-022), every name counts as a file but a file with several hard links
    weighs once, under the extension of its first name."""
    files: dict[str | None, int] = {}
    sizes: dict[str | None, int] = {}
    for row in conn.execute(
        "SELECT extension, COUNT(*) AS files, SUM(size) AS logical_size FROM entry "
        "WHERE scan_id = ? AND kind = 'file' GROUP BY extension",
        (scan_id,),
    ):
        files[row["extension"]] = row["files"]
        sizes[row["extension"]] = row["logical_size"] or 0
    # The other names of a hard-linked file: few, found through the inode index
    # (left to itself, SQLite took 24 s instead of 0.2 s on 400 000 entries).
    for row in conn.execute(
        "SELECT e.extension, SUM(e.size) AS extra FROM entry e "
        "WHERE e.scan_id = ? AND e.kind = 'file' AND e.nlink > 1 AND EXISTS ("
        "SELECT 1 FROM entry i INDEXED BY entry_by_inode WHERE i.scan_id = e.scan_id "
        "AND i.dev = e.dev AND i.ino = e.ino AND i.kind = 'file' AND i.entry_id < e.entry_id) "
        "GROUP BY e.extension",
        (scan_id,),
    ):
        sizes[row["extension"]] -= row["extra"] or 0
    ranked = sorted(files, key=lambda ext: (-sizes[ext], -files[ext], ext or ""))
    return [ExtensionStat(ext, files[ext], sizes[ext]) for ext in ranked[: max(limit, 0)]]


def largest_dirs(conn: sqlite3.Connection, scan_id: int, limit: int = 15) -> list[DirStat]:
    """The folders whose subtree takes the most disk space, the start folder
    excluded. Nested folders all appear: a parent always outweighs its child."""
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
    """The first ``limit`` read errors of a scan, in the order they happened."""
    return list(
        conn.execute(
            "SELECT rel_path_display, operation, errno, message FROM scan_error "
            "WHERE scan_id = ? ORDER BY id LIMIT ?",
            (scan_id, limit),
        )
    )


def list_exclusions(conn: sqlite3.Connection, scan_id: int, limit: int = 50) -> list[sqlite3.Row]:
    """The first ``limit`` paths left out on purpose, in the order they were met."""
    return list(
        conn.execute(
            "SELECT rel_path_display, reason FROM scan_exclusion "
            "WHERE scan_id = ? ORDER BY id LIMIT ?",
            (scan_id, limit),
        )
    )


def forget_scan(conn: sqlite3.Connection, scan_id: int) -> bool:
    """Erase what TreeAudit recorded for one analysis: its index, its duplicate
    analyses and its cached summaries. Never the analysed files (D-043).

    The rows below the scan go with it (foreign keys, ON DELETE CASCADE).
    Returns False if there was no such analysis.
    """
    runs = [row[0] for row in conn.execute("SELECT id FROM dup_run WHERE scan_id = ?", (scan_id,))]
    conn.execute("BEGIN")  # all or nothing: the connection autocommits otherwise
    try:
        for prefix in ("parasite-candidates", "parasites"):
            conn.execute(
                "DELETE FROM summary_cache WHERE key LIKE ? ESCAPE '\\'",
                (f"{prefix}:{scan_id}:%",),
            )
        for run in runs:
            conn.execute(
                "DELETE FROM summary_cache WHERE key LIKE ? ESCAPE '\\'", (f"dupes:{run}:%",)
            )
        deleted = conn.execute("DELETE FROM scan WHERE id = ?", (scan_id,)).rowcount
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return deleted > 0


def compact(conn: sqlite3.Connection) -> None:
    """Give the free space of the index back to the disk (VACUUM)."""
    conn.execute("VACUUM")
