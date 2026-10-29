"""A disk pulled out during a scan or a duplicate analysis, simulated (D-012).

No disk is touched: once "pulled", the filesystem calls fail with EIO, as they
do on a USB disk removed while mounted. Whatever happens, the result must never
be presented as complete.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans
from treeaudit.db.connection import connect_memory
from treeaudit.duplicates.groups import load_groups
from treeaudit.duplicates.runner import DupOptions, run_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner import walker
from treeaudit.scanner.models import ScanStatus
from treeaudit.scanner.runner import ScanRequest, run_scan

KIB = 1024


class Unplug:
    """Counts filesystem calls; after `after` of them, every call fails with EIO."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.calls = 0

    @property
    def pulled(self) -> bool:
        return self.calls >= self.after

    def wrap[**P, R](self, real: Callable[P, R]) -> Callable[P, R]:
        def call(*args: P.args, **kwargs: P.kwargs) -> R:
            if self.pulled:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            self.calls += 1
            return real(*args, **kwargs)

        return call

    def dead(self, real: Callable[..., Any]) -> Callable[..., Any]:
        """Fails once pulled, without counting as a call that brings the pull closer."""

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.pulled:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real(*args, **kwargs)

        return call


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "disk"
    for folder in ("a", "b", "c", "d"):
        for index in range(5):
            path = root / folder / "sub" / f"file-{index}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([index]) * 3 * KIB)
    return root


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def ctx(tmp_path: Path, data_disk_table: Callable[..., MountTable]) -> ValidationContext:
    return ValidationContext(mounts=data_disk_table(str(tmp_path)))


def root_config(root: Path) -> RootConfig:
    return RootConfig(id="usb", path=str(root), label="usb")


def scan(conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> sqlite3.Row:
    outcome = run_scan(conn, ScanRequest(root=root_config(root)), ctx=ctx, cancel=threading.Event())
    row = scans.get_scan(conn, outcome.scan_id)
    assert row is not None
    return row


def pull_during_walk(monkeypatch: pytest.MonkeyPatch, unplug: Unplug) -> None:
    monkeypatch.setattr(walker, "_lstat_at", unplug.wrap(walker._lstat_at))
    monkeypatch.setattr(walker, "_open_dir", unplug.dead(walker._open_dir))
    monkeypatch.setattr(os, "scandir", unplug.dead(os.scandir))


def test_complete_scan_without_the_pull(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext
) -> None:
    row = scan(conn, root, ctx)
    assert row["status"] == "complete"
    assert row["files"] == 20


def test_disk_pulled_during_the_scan_makes_it_incomplete(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    pull_during_walk(monkeypatch, Unplug(after=6))
    row = scan(conn, root, ctx)
    assert row["status"] == "incomplete"
    assert 0 < row["files"] < 20  # what was read before the pull is kept, as a minimum
    assert row["errors"] > 0
    assert {e["errno"] for e in scans.list_errors(conn, row["id"])} == {errno.EIO}


def test_disk_pulled_before_anything_is_listed(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    pull_during_walk(monkeypatch, Unplug(after=0))
    row = scan(conn, root, ctx)
    assert row["status"] == "incomplete"
    assert row["files"] == 0
    [error] = scans.list_errors(conn, row["id"])
    assert error["operation"] == "open_root"


def test_disk_so_dead_that_even_open_descriptors_fail(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opening still works, but asking a just-opened folder what it is fails.
    unplug = Unplug(after=6)
    monkeypatch.setattr(walker, "_lstat_at", unplug.wrap(walker._lstat_at))
    monkeypatch.setattr(os, "fstat", unplug.dead(os.fstat))
    row = scan(conn, root, ctx)
    assert row["status"] == "failed"
    assert "Input/output error" in row["failure"]


def test_disk_pulled_during_the_duplicate_analysis(
    conn: sqlite3.Connection, root: Path, ctx: ValidationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan_id = scan(conn, root, ctx)["id"]
    # Same content in a/, b/, c/ and d/: four copies of each file once fully read.
    unplug = Unplug(after=8)
    monkeypatch.setattr(os, "pread", unplug.wrap(os.pread))
    outcome = run_duplicates(
        conn,
        scan_id,
        root_config(root),
        ctx=ctx,
        options=DupOptions(),
        cancel=threading.Event(),
    )
    assert outcome.status is ScanStatus.INCOMPLETE
    failed = {e["rel_path_display"] for e in dupes_db.list_errors(conn, outcome.run_id)}
    assert failed
    for group in load_groups(conn, outcome.run_id).groups:
        assert not failed & {c.rel_path_display for c in group.copies}
