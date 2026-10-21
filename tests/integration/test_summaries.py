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
from treeaudit.parasites import cached
from treeaudit.parasites.detect import find_parasites
from treeaudit.parasites.rules import load_default_rules
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

    monkeypatch.setattr(summaries, "parasite_findings", must_not_run)
    assert summaries.parasites_summary(conn, scan_id) == first


def test_rules_change_invalidates_the_summary(
    conn: sqlite3.Connection,
    scanned: tuple[int, RootConfig, ValidationContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_id = scanned[0]
    summaries.parasites_summary(conn, scan_id)
    calls: list[int] = []
    real = cached.evaluate_candidates

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cached, "evaluate_candidates", counting)
    monkeypatch.setattr(summaries, "rules_fingerprint", lambda: "another-catalogue")
    monkeypatch.setattr(cached, "rules_fingerprint", lambda: "another-catalogue")
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


@pytest.fixture
def nested(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> int:
    """Green, orange and red findings, some nested in each other."""
    root = tmp_path / "nested"
    files = {
        "web/package.json": b"{}",
        "web/node_modules/lib/package.json": b"{}",
        "web/node_modules/lib/node_modules/x.js": b"x",
        "orphan/node_modules/lib/package.json": b"{}",
        "orphan/node_modules/lib/node_modules/y.js": b"y",
        "py/app/__pycache__/m.pyc": b"\x00",
        "py/app/__pycache__/notes.txt": b"!",
        "site/bootstrap/css/bootstrap.min.css": b"/**/",
        "mac/.DS_Store": b"\x00",
        "dotnet/App.csproj": b"<Project/>",
        "dotnet/bin/App.dll": b"MZ",
    }
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(content)
    ctx = ValidationContext(mounts=data_disk_table(str(tmp_path)))
    outcome = run_scan(
        conn,
        ScanRequest(root=RootConfig(id="n", path=str(root), label="n")),
        ctx=ctx,
        cancel=threading.Event(),
    )
    return outcome.scan_id


@pytest.mark.parametrize("min_confidence", [0, 30, 50, 75, 80, 85, 90, 95, 100])
def test_cached_findings_equal_direct_computation(
    conn: sqlite3.Connection, nested: int, min_confidence: int
) -> None:
    direct = find_parasites(conn, nested, load_default_rules(), min_confidence=min_confidence)
    first = cached.parasite_findings(conn, nested, min_confidence=min_confidence)  # fills
    second = cached.parasite_findings(conn, nested, min_confidence=min_confidence)  # reads
    assert first == direct
    assert second == direct  # same rules, evidence, sizes, recreate options and numbers


def test_cached_findings_are_read_from_the_cache(
    conn: sqlite3.Connection, nested: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached.parasite_findings(conn, nested)

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the cached evaluation should have been used")

    monkeypatch.setattr(cached, "evaluate_candidates", must_not_run)
    assert cached.parasite_findings(conn, nested, min_confidence=0)
