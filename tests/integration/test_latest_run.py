"""The one lookup of "the latest search for duplicates" (db.dupes.latest_run).

It replaced three copies with their own rules: the summary page (any state),
the explorer and the similar folders (finished searches only).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from treeaudit.db import dupes, scans
from treeaudit.db.connection import connect_memory
from treeaudit.scanner.models import ScanStatus
from treeaudit.similar import trees


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


def new_scan(conn: sqlite3.Connection) -> int:
    return scans.create_scan(conn, root_id="r", root_path="/r", subpath="", options={})


def run(
    conn: sqlite3.Connection, scan_id: int, status: ScanStatus | None, **options: object
) -> int:
    run_id = dupes.create_run(conn, scan_id, options)
    if status is not None:  # None: still running
        dupes.finish_run(
            conn, run_id, status=status, candidates=0, files_hashed=0, bytes_read=0, errors=0
        )
    return run_id


def latest_id(conn: sqlite3.Connection, scan_id: int, **kwargs: bool) -> int | None:
    row = dupes.latest_run(conn, scan_id, **kwargs)
    return None if row is None else int(row["id"])


def test_finished_searches_only_by_default_each_kind_apart(conn: sqlite3.Connection) -> None:
    scan = new_scan(conn)
    usable = run(conn, scan, ScanStatus.COMPLETE, min_size=1024)
    small = run(conn, scan, ScanStatus.INCOMPLETE, micro=True)
    run(conn, scan, ScanStatus.FAILED, min_size=1024)
    run(conn, scan, None, micro=True)  # running
    assert latest_id(conn, scan, micro=False) == usable
    assert latest_id(conn, scan, micro=True) == small


def test_the_summary_sees_the_newest_whatever_its_state(conn: sqlite3.Connection) -> None:
    scan = new_scan(conn)
    run(conn, scan, ScanStatus.COMPLETE, min_size=1024)
    failed = run(conn, scan, ScanStatus.FAILED, min_size=1024)
    running = run(conn, scan, None, micro=True)
    assert latest_id(conn, scan, micro=False, finished_only=False) == failed
    assert latest_id(conn, scan, micro=True, finished_only=False) == running


def test_searches_saved_before_micro_existed_are_ordinary_ones(conn: sqlite3.Connection) -> None:
    scan = new_scan(conn)
    old = run(conn, scan, ScanStatus.COMPLETE, min_size=1024)  # no "micro" key at all
    explicit = run(conn, scan, ScanStatus.CANCELLED, micro=False, min_size=1024)
    assert latest_id(conn, scan, micro=False) == old
    assert latest_id(conn, scan, micro=False, finished_only=False) == explicit
    assert latest_id(conn, scan, micro=True) is None


def test_only_the_given_scan_counts(conn: sqlite3.Connection) -> None:
    first, second = new_scan(conn), new_scan(conn)
    mine = run(conn, first, ScanStatus.COMPLETE, min_size=1024)
    run(conn, second, ScanStatus.COMPLETE, min_size=1024)  # newer, but another scan
    assert latest_id(conn, first, micro=False) == mine
    assert latest_id(conn, new_scan(conn), micro=False) is None


def test_far_more_than_500_searches_still_find_the_scans_own(conn: sqlite3.Connection) -> None:
    """The summary page's former copy only looked at the 500 newest searches
    of every scan together: an older scan then showed no search at all."""
    scan, busy = new_scan(conn), new_scan(conn)
    mine = run(conn, scan, ScanStatus.COMPLETE, min_size=1024)
    for _ in range(501):
        run(conn, busy, ScanStatus.COMPLETE, min_size=1024)
    assert latest_id(conn, scan, micro=False, finished_only=False) == mine


def test_similar_folders_read_the_same_search(conn: sqlite3.Connection) -> None:
    scan = new_scan(conn)
    usable = run(conn, scan, ScanStatus.INCOMPLETE, min_size=4096, max_size=10_000)
    run(conn, scan, None, min_size=1)  # running: not usable yet
    found = trees.latest_run(conn, scan)
    assert found is not None
    assert (found.id, found.min_size, found.max_size, found.complete) == (
        usable,
        4096,
        10_000,
        False,
    )
