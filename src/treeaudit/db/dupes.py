"""Reading and writing duplicate analyses. Digests only, never file contents."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from treeaudit.db.connection import from_sqlite_int, to_sqlite_int
from treeaudit.db.scans import now_iso
from treeaudit.scanner.models import ScanStatus


@dataclass(frozen=True, slots=True)
class Candidate:
    """One inode to read (hard links share it and are read once)."""

    entry_id: int
    rel_path: bytes
    rel_path_display: str
    size: int
    dev: int
    ino: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class HashRow:
    entry_id: int
    size: int
    partial: bytes | None
    """Head digest (first EDGE_SIZE bytes)."""
    full: bytes | None
    tail: bytes | None = None


@dataclass(frozen=True, slots=True)
class KnownDigests:
    """Digests of an unchanged file, from an earlier analysis."""

    head: bytes | None
    tail: bytes | None
    full: bytes | None


@dataclass(frozen=True, slots=True)
class DupError:
    entry_id: int
    rel_path_display: str
    operation: str
    errno: int | None
    message: str


def create_run(conn: sqlite3.Connection, scan_id: int, options: dict[str, Any]) -> int:
    cursor = conn.execute(
        "INSERT INTO dup_run (scan_id, status, started_at, options) VALUES (?, ?, ?, ?)",
        (scan_id, ScanStatus.RUNNING.value, now_iso(), json.dumps(options)),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return the new run id")
    return cursor.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: ScanStatus,
    candidates: int,
    files_hashed: int,
    bytes_read: int,
    errors: int,
    reused: int = 0,
    failure: str | None = None,
) -> None:
    if not status.is_final:
        raise ValueError(f"not a final status: {status}")
    conn.execute(
        "UPDATE dup_run SET status = ?, finished_at = ?, candidates = ?, files_hashed = ?, "
        "bytes_read = ?, errors = ?, reused = ?, failure = ? WHERE id = ?",
        (
            status.value,
            now_iso(),
            candidates,
            files_hashed,
            bytes_read,
            errors,
            reused,
            failure,
            run_id,
        ),
    )


def known_digests(
    conn: sqlite3.Connection, candidates: list[Candidate], *, edge_size: int
) -> dict[int, KnownDigests]:
    """Digests computed by earlier analyses for files that did not change.

    A file is considered unchanged when an earlier scan recorded the same
    device, inode, size and modification time (to the nanosecond), the same
    test rsync uses. Only analyses that finished (complete or incomplete) count;
    head and tail digests are reused only if computed with the same edge size.
    """
    known: dict[int, KnownDigests] = {}
    for candidate in candidates:
        row = conn.execute(
            "SELECT h.partial, h.tail, h.full, "
            "json_extract(r.options, '$.edge_size') AS edge FROM entry e "
            "JOIN dup_run r ON r.scan_id = e.scan_id AND r.status IN ('complete', 'incomplete') "
            "JOIN file_hash h ON h.run_id = r.id AND h.entry_id = e.entry_id "
            "WHERE e.dev = ? AND e.ino = ? AND e.size = ? AND e.mtime_ns = ? "
            "ORDER BY r.id DESC LIMIT 1",
            (
                to_sqlite_int(candidate.dev),
                to_sqlite_int(candidate.ino),
                candidate.size,
                candidate.mtime_ns,
            ),
        ).fetchone()
        if row is None:
            continue
        same_edges = row["edge"] == edge_size
        known[candidate.entry_id] = KnownDigests(
            head=row["partial"] if same_edges else None,
            tail=row["tail"] if same_edges else None,
            full=row["full"],
        )
    return known


def save_results(
    conn: sqlite3.Connection, run_id: int, hashes: list[HashRow], errors: list[DupError]
) -> None:
    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT INTO file_hash (run_id, entry_id, size, partial, full, tail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, h.entry_id, h.size, h.partial, h.full, h.tail) for h in hashes],
        )
        conn.executemany(
            "INSERT INTO dup_error (run_id, entry_id, rel_path_display, operation, errno, "
            "message) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, e.entry_id, e.rel_path_display, e.operation, e.errno, e.message)
                for e in errors
            ],
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def fail_interrupted_runs(conn: sqlite3.Connection) -> int:
    """Mark runs left 'running' by a crash as failed. Call while holding the lock."""
    cursor = conn.execute(
        "UPDATE dup_run SET status = ?, finished_at = ?, failure = ? WHERE status IN (?, ?)",
        (
            ScanStatus.FAILED.value,
            now_iso(),
            "interrupted: the process stopped before the analysis finished",
            ScanStatus.RUNNING.value,
            ScanStatus.PENDING.value,
        ),
    )
    return cursor.rowcount


def load_candidates(
    conn: sqlite3.Connection, scan_id: int, *, min_size: int, max_size: int | None
) -> list[Candidate]:
    """One representative per inode, for sizes shared by at least two inodes.

    Read from the index only. Ordered by path so that reading reuses the open
    directory descriptors.
    """
    upper = max_size if max_size is not None else -1
    rows = conn.execute(
        """
        WITH inodes AS (
            SELECT MIN(entry_id) AS entry_id, size, dev, ino
            FROM entry
            WHERE scan_id = ? AND kind = 'file' AND size >= ? AND (? < 0 OR size <= ?)
            GROUP BY dev, ino
        ),
        shared AS (
            SELECT size FROM inodes GROUP BY size HAVING COUNT(*) >= 2
        )
        SELECT e.entry_id, e.rel_path, e.rel_path_display, e.size, e.dev, e.ino, e.mtime_ns
        FROM inodes i
        JOIN shared s ON s.size = i.size
        JOIN entry e ON e.scan_id = ? AND e.entry_id = i.entry_id
        """,
        (scan_id, max(min_size, 1), upper, upper, scan_id),
    )
    # Sorted here rather than with ORDER BY: asking SQLite for path order made it
    # walk the whole scan by path and probe the candidates for every entry
    # (measured: 2 s instead of 0.15 s on 150 000 entries). bytes and BLOB
    # compare the same way.
    candidates = [
        Candidate(
            entry_id=r["entry_id"],
            rel_path=r["rel_path"],
            rel_path_display=r["rel_path_display"],
            size=r["size"],
            dev=from_sqlite_int(r["dev"]),
            ino=from_sqlite_int(r["ino"]),
            mtime_ns=r["mtime_ns"],
        )
        for r in rows
    ]
    candidates.sort(key=lambda c: c.rel_path)
    return candidates


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM dup_run WHERE id = ?", (run_id,)
    ).fetchone()
    return row


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM dup_run ORDER BY id DESC LIMIT ?", (limit,)))


def list_errors(conn: sqlite3.Connection, run_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT rel_path_display, operation, errno, message FROM dup_error "
            "WHERE run_id = ? ORDER BY id LIMIT ?",
            (run_id, limit),
        )
    )
