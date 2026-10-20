"""Schema migrations. Never edit a released migration: append a new one."""

from __future__ import annotations

_V1 = (
    """
    CREATE TABLE scan (
        id              INTEGER PRIMARY KEY,
        root_id         TEXT    NOT NULL,
        root_path       TEXT    NOT NULL,
        subpath         TEXT    NOT NULL DEFAULT '',
        status          TEXT    NOT NULL CHECK (status IN (
                            'pending', 'running', 'complete', 'incomplete', 'cancelled', 'failed'
                        )),
        started_at      TEXT    NOT NULL,
        finished_at     TEXT,
        options         TEXT    NOT NULL,
        entries         INTEGER NOT NULL DEFAULT 0,
        files           INTEGER NOT NULL DEFAULT 0,
        dirs            INTEGER NOT NULL DEFAULT 0,
        symlinks        INTEGER NOT NULL DEFAULT 0,
        others          INTEGER NOT NULL DEFAULT 0,
        logical_size    INTEGER NOT NULL DEFAULT 0,
        allocated_size  INTEGER NOT NULL DEFAULT 0,
        errors          INTEGER NOT NULL DEFAULT 0,
        exclusions      INTEGER NOT NULL DEFAULT 0,
        failure         TEXT
    )
    """,
    """
    CREATE TABLE entry (
        scan_id           INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
        entry_id          INTEGER NOT NULL,
        parent_id         INTEGER,
        depth             INTEGER NOT NULL,
        name              BLOB    NOT NULL,
        name_display      TEXT    NOT NULL,
        name_valid_utf8   INTEGER NOT NULL,
        rel_path          BLOB    NOT NULL,
        rel_path_display  TEXT    NOT NULL,
        kind              TEXT    NOT NULL CHECK (kind IN ('file', 'dir', 'symlink', 'other')),
        extension         TEXT,
        size              INTEGER NOT NULL,
        allocated_size    INTEGER NOT NULL,
        mtime_ns          INTEGER NOT NULL,
        mode              INTEGER NOT NULL,
        uid               INTEGER NOT NULL,
        gid               INTEGER NOT NULL,
        dev               INTEGER NOT NULL,
        ino               INTEGER NOT NULL,
        nlink             INTEGER NOT NULL,
        link_target       BLOB,
        PRIMARY KEY (scan_id, entry_id)
    )
    """,
    "CREATE INDEX entry_by_parent ON entry (scan_id, parent_id)",
    "CREATE INDEX entry_by_extension ON entry (scan_id, kind, extension)",
    """
    CREATE TABLE dir_summary (
        scan_id         INTEGER NOT NULL,
        entry_id        INTEGER NOT NULL,
        files           INTEGER NOT NULL,
        dirs            INTEGER NOT NULL,
        symlinks        INTEGER NOT NULL,
        others          INTEGER NOT NULL,
        logical_size    INTEGER NOT NULL,
        allocated_size  INTEGER NOT NULL,
        errors          INTEGER NOT NULL,
        PRIMARY KEY (scan_id, entry_id),
        FOREIGN KEY (scan_id, entry_id) REFERENCES entry(scan_id, entry_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX dir_summary_by_size ON dir_summary (scan_id, allocated_size)",
    """
    CREATE TABLE scan_error (
        id                INTEGER PRIMARY KEY,
        scan_id           INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
        rel_path          BLOB    NOT NULL,
        rel_path_display  TEXT    NOT NULL,
        operation         TEXT    NOT NULL,
        errno             INTEGER,
        message           TEXT    NOT NULL
    )
    """,
    "CREATE INDEX scan_error_by_scan ON scan_error (scan_id)",
    """
    CREATE TABLE scan_exclusion (
        id                INTEGER PRIMARY KEY,
        scan_id           INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
        rel_path          BLOB    NOT NULL,
        rel_path_display  TEXT    NOT NULL,
        reason            TEXT    NOT NULL
    )
    """,
    "CREATE INDEX scan_exclusion_by_scan ON scan_exclusion (scan_id)",
)

# v2: lookups by name (rule candidates) and by path prefix (subtree evidence).
_V2 = (
    "CREATE INDEX entry_by_name ON entry (scan_id, name)",
    "CREATE INDEX entry_by_path ON entry (scan_id, rel_path)",
)

# v3: duplicate analyses (D-015). Only digests are stored, never file contents.
_V3 = (
    """
    CREATE TABLE dup_run (
        id              INTEGER PRIMARY KEY,
        scan_id         INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
        status          TEXT    NOT NULL CHECK (status IN (
                            'pending', 'running', 'complete', 'incomplete', 'cancelled', 'failed'
                        )),
        started_at      TEXT    NOT NULL,
        finished_at     TEXT,
        options         TEXT    NOT NULL,
        candidates      INTEGER NOT NULL DEFAULT 0,
        files_hashed    INTEGER NOT NULL DEFAULT 0,
        bytes_read      INTEGER NOT NULL DEFAULT 0,
        errors          INTEGER NOT NULL DEFAULT 0,
        failure         TEXT
    )
    """,
    """
    CREATE TABLE file_hash (
        run_id          INTEGER NOT NULL REFERENCES dup_run(id) ON DELETE CASCADE,
        entry_id        INTEGER NOT NULL,
        size            INTEGER NOT NULL,
        partial         BLOB,
        full            BLOB,
        PRIMARY KEY (run_id, entry_id)
    )
    """,
    "CREATE INDEX file_hash_by_digest ON file_hash (run_id, size, full)",
    """
    CREATE TABLE dup_error (
        id                INTEGER PRIMARY KEY,
        run_id            INTEGER NOT NULL REFERENCES dup_run(id) ON DELETE CASCADE,
        entry_id          INTEGER NOT NULL,
        rel_path_display  TEXT    NOT NULL,
        operation         TEXT    NOT NULL,
        errno             INTEGER,
        message           TEXT    NOT NULL
    )
    """,
    "CREATE INDEX dup_error_by_run ON dup_error (run_id)",
    "CREATE INDEX entry_by_inode ON entry (scan_id, dev, ino)",
)

# v4: reuse digests across analyses (same inode, size and mtime -> same content),
# head/tail digests kept so that a second analysis reads almost nothing.
_V4 = (
    "CREATE INDEX entry_by_identity ON entry (dev, ino)",
    # Measured on 1 000 000 entries: the path index was ~21 % of the database and
    # slowed every insert; subtree lookups now follow the parent links instead.
    # Extension statistics scan the whole scan anyway.
    "DROP INDEX entry_by_path",
    "DROP INDEX entry_by_extension",
    "ALTER TABLE dup_run ADD COLUMN reused INTEGER NOT NULL DEFAULT 0",
    # file_hash.partial now holds the head digest (first 4 KiB) and tail the last 4 KiB.
    "ALTER TABLE file_hash ADD COLUMN tail BLOB",
    # Summaries computed once for finished (immutable) scans and analyses.
    """
    CREATE TABLE summary_cache (
        key         TEXT PRIMARY KEY,
        payload     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1, _V2, _V3, _V4)
