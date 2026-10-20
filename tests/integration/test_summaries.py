"""Cached summaries of the summary page."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db import scans
from treeaudit.db.connection import connect_memory
from treeaudit.duplicates.runner import DupOptions, run_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.runner import ScanRequest, run_scan
from treeaudit.web import summaries


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def scanned(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> tuple[int, RootConfig, ValidationContext]:
    root = tmp_path / "root"
    (root / "app" / "node_modules" / "dep").mkdir(parents=True)
    (root / "app" / "package.json").write_bytes(b"{}")
    (root / "app" / "node_modules" / "dep" / "i.js").write_bytes(b"x")
    for name in "ab":
        (root / name).write_bytes(b"d" * 5000)
    config = RootConfig(id="r", path=str(root), label="r")
    ctx = ValidationContext(mounts=data_disk_table(str(tmp_path)))
    outcome = run_scan(conn, ScanRequest(root=config), ctx=ctx, cancel=threading.Event())
    return outcome.scan_id, config, ctx


def test_parasites_summary_is_computed_once(
    conn: sqlite3.Connection,
    scanned: tuple[int, RootConfig, ValidationContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_id = scanned[0]
    first = summaries.parasites_summary(conn, scan_id)
    assert first["safe_count"] == 1

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the cached summary should have been used")

    monkeypatch.setattr(summaries, "find_parasites", must_not_run)
    assert summaries.parasites_summary(conn, scan_id) == first


def test_rules_change_invalidates_the_summary(
    conn: sqlite3.Connection,
    scanned: tuple[int, RootConfig, ValidationContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_id = scanned[0]
    summaries.parasites_summary(conn, scan_id)
    calls: list[int] = []
    real = summaries.find_parasites

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(summaries, "find_parasites", counting)
    monkeypatch.setattr(summaries, "rules_fingerprint", lambda: "another-catalogue")
    summaries.parasites_summary(conn, scan_id)
    assert calls == [1]


def test_unfinished_scan_is_not_cached(
    conn: sqlite3.Connection, scanned: tuple[int, RootConfig, ValidationContext]
) -> None:
    scan_id = scanned[0]
    conn.execute("UPDATE scan SET status = 'running' WHERE id = ?", (scan_id,))
    summaries.parasites_summary(conn, scan_id)
    assert conn.execute("SELECT COUNT(*) FROM summary_cache").fetchone()[0] == 0
    assert scans.get_scan(conn, scan_id) is not None


def test_dupes_summary_cached(
    conn: sqlite3.Connection,
    scanned: tuple[int, RootConfig, ValidationContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_id, root, ctx = scanned
    outcome = run_duplicates(
        conn, scan_id, root, ctx=ctx, options=DupOptions(), cancel=threading.Event()
    )
    first = summaries.dupes_summary(conn, outcome.run_id)
    assert first == {"groups": 1, "removable": 1, "recoverable": 5000}
    monkeypatch.setattr(
        summaries, "load_groups", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert summaries.dupes_summary(conn, outcome.run_id) == first
