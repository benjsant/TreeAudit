"""Scan runner and index tests: statuses, totals, persistence, recovery."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ScanLock, ScanLockedError
from treeaudit.db import scans
from treeaudit.db.connection import (
    connect,
    connect_memory,
    database_path,
    from_sqlite_int,
    to_sqlite_int,
)
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import RootRejectedError, ValidationContext
from treeaudit.scanner.models import DirDone, ScanStatus
from treeaudit.scanner.runner import ScanRequest, execute_scan, run_scan
from treeaudit.scanner.walker import SubpathError, WalkOptions, walk

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")

SECRET = b"TOP-SECRET-CONTENT-7f3a9c"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "docs" / "old").mkdir(parents=True)
    (root / "docs" / "report.pdf").write_bytes(b"p" * 4000)
    (root / "docs" / "old" / "notes.txt").write_bytes(SECRET)
    (root / "photo.JPG").write_bytes(b"j" * 9000)
    (root / ".bashrc").write_bytes(b"b")
    return root


@pytest.fixture
def ctx(tmp_path: Path, data_disk_table: Callable[..., MountTable]) -> ValidationContext:
    return ValidationContext(mounts=data_disk_table(str(tmp_path)), data_dir=str(tmp_path / "data"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


def request_for(root: Path, **kwargs: object) -> ScanRequest:
    return ScanRequest(root=RootConfig(id="r", path=str(root), label="r"), **kwargs)  # type: ignore[arg-type]


def scan(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, **kwargs: object
) -> sqlite3.Row:
    cancel = kwargs.pop("cancel", threading.Event())
    outcome = run_scan(conn, request_for(root, **kwargs), ctx=ctx, cancel=cancel)  # type: ignore[arg-type]
    row = scans.get_scan(conn, outcome.scan_id)
    assert row is not None
    return row


def test_complete_scan(conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> None:
    row = scan(conn, root, ctx)
    assert row["status"] == "complete"
    assert (row["files"], row["dirs"], row["errors"]) == (4, 2, 0)
    assert row["logical_size"] == 4000 + len(SECRET) + 9000 + 1
    assert row["entries"] == 7  # root + 2 dirs + 4 files
    assert (
        conn.execute("SELECT COUNT(*) FROM entry WHERE scan_id = ?", (row["id"],)).fetchone()[0]
        == 7
    )
    assert row["finished_at"] is not None


def test_sql_totals_match_walker_totals(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext
) -> None:
    os.link(root / "photo.JPG", root / "docs" / "photo-again.JPG")
    row = scan(conn, root, ctx)
    [done] = [e for e in walk(WalkOptions(root_path=str(root))) if isinstance(e, DirDone)][-1:]
    assert row["files"] == done.totals.files
    assert row["logical_size"] == done.totals.logical_size
    assert row["allocated_size"] == done.totals.allocated_size


def test_extensions_and_largest_dirs(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext
) -> None:
    scan_id = scan(conn, root, ctx)["id"]
    stats = {s.extension: (s.files, s.logical_size) for s in scans.top_extensions(conn, scan_id)}
    assert stats["jpg"] == (1, 9000)  # lower-cased
    assert stats["pdf"] == (1, 4000)
    assert stats[None] == (1, 1)  # .bashrc has no extension
    largest = scans.largest_dirs(conn, scan_id)
    assert [d.rel_path_display for d in largest][:1] == ["docs"]


@non_root_only
def test_permission_error_makes_scan_incomplete(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext
) -> None:
    (root / "docs" / "old").chmod(0o000)
    try:
        row = scan(conn, root, ctx)
    finally:
        (root / "docs" / "old").chmod(0o700)
    assert row["status"] == "incomplete"
    assert row["errors"] == 1
    [error] = scans.list_errors(conn, row["id"])
    assert (error["rel_path_display"], error["operation"]) == ("docs/old", "open_dir")


def test_cancelled_scan_keeps_partial_results(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext
) -> None:
    cancel = threading.Event()
    cancel.set()
    row = scan(conn, root, ctx, cancel=cancel)
    assert row["status"] == "cancelled"
    assert row["entries"] == 1  # stopped right after the first event
    assert row["finished_at"] is not None


def test_internal_failure_marks_scan_failed(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: scans.ScanWriter, record: object) -> None:
        raise RuntimeError("disk full (simulated)")

    monkeypatch.setattr(scans.ScanWriter, "add_entry", broken)
    row = scan(conn, root, ctx)
    assert row["status"] == "failed"
    assert "disk full (simulated)" in row["failure"]


def test_small_batches(conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> None:
    for index in range(50):
        (root / f"f{index}").write_bytes(b"")
    outcome = run_scan(conn, request_for(root), ctx=ctx, cancel=threading.Event(), batch_size=3)
    count = conn.execute(
        "SELECT COUNT(*) FROM entry WHERE scan_id = ?", (outcome.scan_id,)
    ).fetchone()[0]
    assert count == outcome.entries == 57


def test_subfolder_scan(conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> None:
    row = scan(conn, root, ctx, subpath="docs")
    assert row["subpath"] == "docs"
    assert row["files"] == 2
    paths = {
        r[0] for r in conn.execute("SELECT rel_path FROM entry WHERE scan_id = ?", (row["id"],))
    }
    assert paths == {b"docs", b"docs/report.pdf", b"docs/old", b"docs/old/notes.txt"}


def test_unsafe_subfolder_creates_no_scan(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext
) -> None:
    with pytest.raises(SubpathError):
        run_scan(conn, request_for(root, subpath="../.."), ctx=ctx, cancel=threading.Event())
    assert scans.list_scans(conn) == []


def test_rejected_root_creates_no_scan(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, tmp_path: Path
) -> None:
    (tmp_path / "alias").symlink_to(root)
    with pytest.raises(RootRejectedError):
        run_scan(conn, request_for(tmp_path / "alias"), ctx=ctx, cancel=threading.Event())
    assert scans.list_scans(conn) == []


def test_interrupted_scans_are_marked_failed(conn: sqlite3.Connection) -> None:
    scan_id = scans.create_scan(conn, root_id="r", root_path="/x", subpath="", options={})
    assert scans.fail_interrupted_scans(conn) == 1
    row = scans.get_scan(conn, scan_id)
    assert row is not None
    assert row["status"] == "failed"
    assert "interrupted" in row["failure"]


# --- persistence and privacy ----------------------------------------------------------


def test_database_file_is_private_and_holds_no_content(
    tmp_path: Path, root: Path, ctx: ValidationContext
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    connection = connect(str(data_dir))
    try:
        run_scan(connection, request_for(root), ctx=ctx, cancel=threading.Event())
    finally:
        connection.close()
    path = database_path(str(data_dir))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for item in data_dir.iterdir():
        assert SECRET not in item.read_bytes(), item.name
    # ...while the file name itself is indexed.
    check = connect(str(data_dir))
    try:
        assert (
            check.execute("SELECT COUNT(*) FROM entry WHERE name = ?", (b"notes.txt",)).fetchone()[
                0
            ]
            == 1
        )
    finally:
        check.close()


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1, 2**63, 2**64 - 1])
def test_unsigned_64_bit_round_trip(value: int) -> None:
    stored = to_sqlite_int(value)
    assert -(2**63) <= stored < 2**63
    assert from_sqlite_int(stored) == value


@pytest.mark.parametrize("value", [-1, 2**64])
def test_out_of_range_integers(value: int) -> None:
    with pytest.raises(ValueError):
        to_sqlite_int(value)


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    connection = connect(str(tmp_path))
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        connect(str(tmp_path))


def test_extension_rules() -> None:
    assert scans.extension_of(b"archive.TAR.GZ") == "gz"
    assert scans.extension_of(b".bashrc") is None
    assert scans.extension_of(b"noext") is None
    assert scans.extension_of(b"trailing.") is None
    assert scans.extension_of(b"x." + b"a" * 40) is None


def test_display_of_invalid_utf8() -> None:
    assert scans.display(b"ok") == ("ok", True)
    assert scans.display(b"bad\xff") == ("bad\\xff", False)


# --- lock and end-to-end ------------------------------------------------------------------


def test_only_one_scan_at_a_time(tmp_path: Path) -> None:
    with ScanLock(str(tmp_path)), pytest.raises(ScanLockedError):
        ScanLock(str(tmp_path)).acquire()
    with ScanLock(str(tmp_path)):  # released: can be taken again
        pass


def test_execute_scan_end_to_end(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="r", path=str(root), label="r"),),
    )
    outcome = execute_scan(
        config,
        ScanRequest(root=config.roots[0]),
        mounts=data_disk_table(str(tmp_path)),
        home=None,
        cancel=threading.Event(),
    )
    assert outcome.status is ScanStatus.COMPLETE
    assert outcome.totals.files == 4
    assert database_path(config.data_dir).is_file()


def test_migration_from_v1_adds_lookup_indexes(tmp_path: Path) -> None:
    from treeaudit.db.connection import migrate
    from treeaudit.db.schema import MIGRATIONS

    raw = sqlite3.connect(tmp_path / "old.sqlite3", isolation_level=None)
    for statement in MIGRATIONS[0]:
        raw.execute(statement)
    raw.execute("PRAGMA user_version = 1")
    migrate(raw)
    indexes = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"entry_by_name", "entry_by_path"} <= indexes
    assert raw.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    raw.close()
