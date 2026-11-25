"""Signatures of the images of an analysis, for similar images (D-069).

The page decodes each image in the browser and sends back a gradient hash and a
32 x 32 grey thumbnail, or why it could not decode it. Nothing else of the
image is stored. A signature computed for a file that did not change since
(same device, inode, size and modification time, as for duplicates) is reused.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass

ALGO = 1
"""Version of the signature: a page computing another one is refused."""
HASH_BYTES = 32
THUMB_BYTES = 32 * 32
FAILURES = ("undecodable", "unreadable")
"""Why the page has no signature: the browser could not decode the image, or
the server refused to send it (changed, unreadable since the analysis)."""


@dataclass(frozen=True, slots=True)
class Signature:
    """What the page computed for one image."""

    entry_id: int
    hash: bytes
    thumb: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Failure:
    """An image the page could not sign, and why (one of FAILURES)."""

    entry_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class Pending:
    """Where the signatures of an analysis stand."""

    total: int
    done: int
    failed: int
    todo: tuple[int, ...]
    """Entry ids of the images still to sign, in path order."""


def _candidates_sql(extensions: Collection[str]) -> str:
    marks = ",".join("?" * len(extensions))
    return (
        "e.scan_id = ? AND e.kind = 'file' AND e.size > 0 AND e.size <= ? "
        f"AND e.extension IN ({marks})"
    )


def candidate_ids(
    conn: sqlite3.Connection, scan_id: int, *, extensions: Collection[str], max_size: int
) -> set[int]:
    """The images of an analysis worth signing: from the index alone (the
    content decides later, when the page decodes them)."""
    sql = f"SELECT e.entry_id FROM entry e WHERE {_candidates_sql(extensions)}"  # noqa: S608
    return {row[0] for row in conn.execute(sql, (scan_id, max_size, *sorted(extensions)))}


def reuse_known(
    conn: sqlite3.Connection, scan_id: int, *, extensions: Collection[str], max_size: int
) -> int:
    """Copy the signatures computed by earlier analyses for files that did not
    change; failures are not copied (a newer browser may decode the image).
    Returns how many were reused."""
    # Forced join order, as for duplicate digests (db/dupes.py): the identity
    # index first, never a scan of every signature for every image.
    sql = (
        "INSERT OR IGNORE INTO image_sig "  # noqa: S608 - placeholders only
        "(scan_id, entry_id, algo, hash, thumb, width, height) "
        "SELECT e.scan_id, e.entry_id, s.algo, s.hash, s.thumb, s.width, s.height "
        "FROM entry e "
        "CROSS JOIN entry o INDEXED BY entry_by_identity ON o.dev = e.dev AND o.ino = e.ino "
        "AND o.size = e.size AND o.mtime_ns = e.mtime_ns AND o.scan_id != e.scan_id "
        "CROSS JOIN image_sig s ON s.scan_id = o.scan_id AND s.entry_id = o.entry_id "
        "AND s.algo = ? AND s.failure IS NULL "
        f"WHERE {_candidates_sql(extensions)} "
        "AND NOT EXISTS (SELECT 1 FROM image_sig x "
        "WHERE x.scan_id = e.scan_id AND x.entry_id = e.entry_id)"
    )
    cursor = conn.execute(sql, (ALGO, scan_id, max_size, *sorted(extensions)))
    return cursor.rowcount


def pending(
    conn: sqlite3.Connection, scan_id: int, *, extensions: Collection[str], max_size: int
) -> Pending:
    """What is left to sign, after reusing what earlier analyses computed.
    Signatures of another algorithm version count as not done."""
    reuse_known(conn, scan_id, extensions=extensions, max_size=max_size)
    sql = (
        "SELECT e.entry_id, s.algo, s.failure FROM entry e "  # noqa: S608 - placeholders only
        "LEFT JOIN image_sig s ON s.scan_id = e.scan_id AND s.entry_id = e.entry_id "
        f"WHERE {_candidates_sql(extensions)} ORDER BY e.rel_path"
    )
    total = done = failed = 0
    todo: list[int] = []
    for entry_id, algo, failure in conn.execute(sql, (scan_id, max_size, *sorted(extensions))):
        total += 1
        if algo != ALGO:
            todo.append(entry_id)
        elif failure is None:
            done += 1
        else:
            failed += 1
    return Pending(total, done, failed, tuple(todo))


def save(
    conn: sqlite3.Connection,
    scan_id: int,
    signatures: Sequence[Signature],
    failures: Sequence[Failure],
    *,
    extensions: Collection[str],
    max_size: int,
) -> list[int]:
    """Store what the page sent, all or nothing. Returns the entry ids that are
    not images of this analysis; then nothing is stored. Sizes and reasons are
    checked here too: the page is not trusted."""
    allowed = candidate_ids(conn, scan_id, extensions=extensions, max_size=max_size)
    sent = [s.entry_id for s in signatures] + [f.entry_id for f in failures]
    refused = sorted({entry_id for entry_id in sent if entry_id not in allowed})
    if refused:
        return refused
    for signature in signatures:
        if len(signature.hash) != HASH_BYTES or len(signature.thumb) != THUMB_BYTES:
            raise ValueError("signature of the wrong size")
        if signature.width < 1 or signature.height < 1:
            raise ValueError("image without pixels")
    if any(failure.reason not in FAILURES for failure in failures):
        raise ValueError("unknown failure reason")
    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO image_sig "
            "(scan_id, entry_id, algo, hash, thumb, width, height, failure) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            [(scan_id, s.entry_id, ALGO, s.hash, s.thumb, s.width, s.height) for s in signatures],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO image_sig (scan_id, entry_id, algo, failure) "
            "VALUES (?, ?, ?, ?)",
            [(scan_id, f.entry_id, ALGO, f.reason) for f in failures],
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return []
