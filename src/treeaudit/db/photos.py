"""Dates the photos were taken, read from their EXIF metadata (D-071).

The job of photos/runner.py reads them for the photos below a folder of an
analysis; Ranger uses them. A row is stored for every photo read: its date, no
date (the file has none), or why it could not be read. A date read for a file
that did not change since (same device, inode, size and modification time, as
for duplicates) is reused by later analyses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from treeaudit.db.connection import from_sqlite_int

READER = 1
"""Version of the reading (photos/exif.py): rows of another version are read
again, so a reader that learns a new format finds the dates it missed."""
FAILURES = ("unreadable", "changed")
"""Why a photo could not be read: the file could not be opened or read, or it
is not the one of the analysis any more. Both are asked for again next time:
a disk can come back, a permission be given."""
_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class Photo:
    """A photo to read, as the analysis recorded it."""

    entry_id: int
    rel_path: bytes
    size: int
    dev: int
    ino: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class Result:
    """What reading one photo gave: a date, no date (``taken`` None), or a
    failure (one of FAILURES)."""

    entry_id: int
    taken: datetime | None
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class Pending:
    """Where the dates of the photos below a folder stand."""

    total: int
    dated: int
    undated: int
    failed: int
    todo: tuple[Photo, ...]
    """Photos never read by this reader version, or whose reading failed, in
    path order."""


def _subtree_sql(extensions: Collection[str], columns: str) -> str:
    """The photos below a folder (itself included), with their stored row.
    Parameters: start id, scan id, scan id, the extensions."""
    marks = ",".join("?" * len(extensions))
    return (
        "WITH RECURSIVE sub(id) AS ("  # noqa: S608 - placeholders only
        "  SELECT ? UNION ALL"
        "  SELECT e.entry_id FROM sub CROSS JOIN entry e INDEXED BY entry_by_parent"
        "  ON e.scan_id = ? AND e.parent_id = sub.id"
        ") "
        f"SELECT {columns} FROM sub CROSS JOIN entry e ON e.scan_id = ? AND e.entry_id = sub.id "
        "LEFT JOIN photo_date d ON d.scan_id = e.scan_id AND d.entry_id = e.entry_id "
        f"WHERE e.kind = 'file' AND e.size > 0 AND e.extension IN ({marks})"
    )


def reuse_known(conn: sqlite3.Connection, scan_id: int, *, extensions: Collection[str]) -> int:
    """Copy the rows read by earlier analyses for files that did not change;
    failures are not copied. Returns how many were reused."""
    marks = ",".join("?" * len(extensions))
    # Forced join order, as for image signatures (db/images.py).
    sql = (
        "INSERT OR IGNORE INTO photo_date (scan_id, entry_id, reader, taken) "  # noqa: S608
        "SELECT e.scan_id, e.entry_id, d.reader, d.taken "
        "FROM entry e "
        "CROSS JOIN entry o INDEXED BY entry_by_identity ON o.dev = e.dev AND o.ino = e.ino "
        "AND o.size = e.size AND o.mtime_ns = e.mtime_ns AND o.scan_id != e.scan_id "
        "CROSS JOIN photo_date d ON d.scan_id = o.scan_id AND d.entry_id = o.entry_id "
        "AND d.reader = ? AND d.failure IS NULL "
        f"WHERE e.scan_id = ? AND e.kind = 'file' AND e.size > 0 AND e.extension IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM photo_date x "
        "WHERE x.scan_id = e.scan_id AND x.entry_id = e.entry_id)"
    )
    cursor = conn.execute(sql, (READER, scan_id, *sorted(extensions)))
    return cursor.rowcount


def pending(
    conn: sqlite3.Connection, scan_id: int, start_id: int, *, extensions: Collection[str]
) -> Pending:
    """What is left to read below a folder, after reusing what earlier
    analyses read. Writes to the index (the reused rows)."""
    reuse_known(conn, scan_id, extensions=extensions)
    columns = (
        "e.entry_id, e.rel_path, e.size, e.dev, e.ino, e.mtime_ns, d.reader, d.taken, d.failure"
    )
    sql = _subtree_sql(extensions, columns)
    total = dated = undated = failed = 0
    todo: list[Photo] = []
    for row in conn.execute(sql, (start_id, scan_id, scan_id, *sorted(extensions))):
        total += 1
        reader, taken, failure = row[6], row[7], row[8]
        if reader != READER or failure is not None:
            if failure is not None and reader == READER:
                failed += 1
            todo.append(
                Photo(
                    entry_id=row[0],
                    rel_path=bytes(row[1]),
                    size=row[2],
                    dev=from_sqlite_int(row[3]),
                    ino=from_sqlite_int(row[4]),
                    mtime_ns=row[5],
                )
            )
        elif taken is None:
            undated += 1
        else:
            dated += 1
    # Sorted here, as for duplicates: asking SQLite for path order is slower.
    todo.sort(key=lambda photo: photo.rel_path)
    return Pending(total, dated, undated, failed, tuple(todo))


@dataclass(frozen=True, slots=True)
class Counts:
    """How far the dates of the photos below a folder were read."""

    total: int
    dated: int
    undated: int
    failed: int

    @property
    def unread(self) -> int:
        """Photos never read by this reader version (failures not included)."""
        return self.total - self.dated - self.undated - self.failed


def counts(
    conn: sqlite3.Connection, scan_id: int, start_id: int, *, extensions: Collection[str]
) -> Counts:
    """Where the dates below a folder stand, without writing anything (a page
    shows it; the job reuses the dates of earlier analyses when it starts)."""
    sql = _subtree_sql(extensions, "d.reader, d.taken, d.failure")
    total = dated = undated = failed = 0
    for reader, taken, failure in conn.execute(
        sql, (start_id, scan_id, scan_id, *sorted(extensions))
    ):
        total += 1
        if reader != READER:
            continue
        if failure is not None:
            failed += 1
        elif taken is None:
            undated += 1
        else:
            dated += 1
    return Counts(total, dated, undated, failed)


def save(conn: sqlite3.Connection, scan_id: int, results: Sequence[Result]) -> None:
    """Store what reading gave, in one transaction; a row read again replaces
    the previous one."""
    if any(r.failure is not None and (r.failure not in FAILURES or r.taken) for r in results):
        raise ValueError("unknown failure reason, or a failure with a date")
    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO photo_date (scan_id, entry_id, reader, taken, failure) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    scan_id,
                    r.entry_id,
                    READER,
                    None if r.taken is None else r.taken.strftime(_FORMAT),
                    r.failure,
                )
                for r in results
            ],
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def taken_dates(
    conn: sqlite3.Connection, scan_id: int, entry_ids: Iterable[int]
) -> dict[int, datetime]:
    """The dates read for these entries, by entry id (those without a date, or
    read by another reader version, are left out)."""
    found: dict[int, datetime] = {}
    ids = list(entry_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT entry_id, taken FROM photo_date "  # noqa: S608 - placeholders only
            f"WHERE scan_id = ? AND reader = ? AND taken IS NOT NULL AND entry_id IN ({marks})",
            (scan_id, READER, *chunk),
        )
        for entry_id, taken in rows:
            found[entry_id] = datetime.strptime(taken, _FORMAT)
    return found
