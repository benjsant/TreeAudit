"""Parasite detection on real temporary trees, scanned then analysed from the index."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db.connection import connect_memory
from treeaudit.parasites.detect import Band, Finding, find_parasites
from treeaudit.parasites.rules import load_default_rules
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.runner import ScanRequest, run_scan

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")

RULES = load_default_rules()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def analyse(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> Callable[..., dict[str, Finding]]:
    """Scan tmp_path/root and return findings keyed by relative path."""
    ctx = ValidationContext(mounts=data_disk_table(str(tmp_path)))

    def run(min_confidence: int = 0) -> dict[str, Finding]:
        outcome = run_scan(
            conn,
            ScanRequest(root=RootConfig(id="r", path=str(tmp_path / "root"), label="r")),
            ctx=ctx,
            cancel=threading.Event(),
        )
        findings = find_parasites(conn, outcome.scan_id, RULES, min_confidence=min_confidence)
        return {f.rel_path_display: f for f in findings}

    return run


def touch(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


# --- confirmed vs unconfirmed ---------------------------------------------------------


def test_python_venv_needs_pyvenv_cfg(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "real" / ".venv" / "pyvenv.cfg", b"home = /usr/bin")
    touch(root / "real" / "requirements.txt", b"x")
    touch(root / "notes" / "venv" / "important.txt", b"keep me")
    found = analyse()
    assert found["real/.venv"].band is Band.GREEN
    assert found["real/.venv"].recreate is not None
    assert "requirements.txt" in found["real/.venv"].recreate.command
    assert found["notes/venv"].confidence < 30  # hidden with the default threshold


def test_default_threshold_hides_weak_candidates(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "notes" / "venv" / "important.txt", b"keep me")
    touch(root / "scripts" / "bin" / "backup.sh", b"#!/bin/sh")
    assert analyse(min_confidence=30) == {}


def test_pycache_with_foreign_file_is_downgraded(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "clean" / "__pycache__" / "m.cpython-313.pyc")
    touch(root / "odd" / "__pycache__" / "m.cpython-313.pyc")
    touch(root / "odd" / "__pycache__" / "my-notes.txt", b"!")
    found = analyse()
    assert found["clean/__pycache__"].confidence == 100
    # 85 (base) - 50 (a non-.pyc file inside): a personal file in a cache means doubt.
    assert found["odd/__pycache__"].confidence == 35
    assert found["odd/__pycache__"].band is Band.RED


def test_node_modules_with_and_without_package_json(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "app" / "package.json", b"{}")
    touch(root / "app" / "package-lock.json", b"{}")
    touch(root / "app" / "node_modules" / "dep" / "index.js")
    touch(root / "orphan" / "node_modules" / "dep" / "index.js")
    found = analyse()
    assert found["app/node_modules"].confidence == 90
    assert found["app/node_modules"].recreate is not None
    assert found["app/node_modules"].recreate.command == "npm ci"
    assert found["orphan/node_modules"].confidence == 50


def test_dotnet_bin_only_with_a_project(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "proj" / "App.csproj", b"<Project />")
    touch(root / "proj" / "bin" / "App.dll")
    touch(root / "home" / "bin" / "script.sh")
    found = analyse()
    assert found["proj/bin"].confidence == 75
    assert found["home/bin"].confidence == 10


def test_most_confident_rule_wins(root: Path, analyse: Callable[..., dict[str, Finding]]) -> None:
    touch(root / "crate" / "Cargo.toml", b"[package]")
    touch(root / "crate" / "target" / "debug" / "app")
    found = analyse()
    assert found["crate/target"].rule.id == "rust-target"
    assert found["crate/target"].confidence == 85


def test_confidence_cap(root: Path, analyse: Callable[..., dict[str, Finding]]) -> None:
    touch(root / "site" / "bootstrap" / "css" / "bootstrap.min.css")
    found = analyse()
    assert found["site/bootstrap"].confidence == 40
    assert found["site/bootstrap"].confidence <= found["site/bootstrap"].rule.max_confidence


def test_file_rules_and_globs(root: Path, analyse: Callable[..., dict[str, Finding]]) -> None:
    touch(root / "photos" / ".DS_Store")
    touch(root / "photos" / "._IMG_1.jpg")
    touch(root / "thesis" / "main.tex", b"\\documentclass{article}")
    touch(root / "thesis" / "main.aux")
    touch(root / "lonely" / "data.aux")
    found = analyse()
    assert found["photos/.DS_Store"].confidence == 95
    assert found["photos/._IMG_1.jpg"].rule.id == "macos-appledouble"
    assert found["thesis/main.aux"].confidence == 85
    assert found["lonely/data.aux"].confidence == 20


def test_symlink_named_like_a_parasite_is_ignored(
    root: Path, analyse: Callable[..., dict[str, Finding]], tmp_path: Path
) -> None:
    touch(root / "app" / "package.json", b"{}")
    (root / "app" / "node_modules").symlink_to(tmp_path)
    assert "app/node_modules" not in analyse()


# --- nesting, sizes, ordering -------------------------------------------------------------


def test_nested_parasites_inside_a_green_one_are_not_repeated(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "app" / "package.json", b"{}")
    touch(root / "app" / "node_modules" / "lib" / "package.json", b"{}")
    touch(root / "app" / "node_modules" / "lib" / "node_modules" / "x.js")
    touch(root / "orphan" / "node_modules" / "lib" / "package.json", b"{}")
    touch(root / "orphan" / "node_modules" / "lib" / "node_modules" / "x.js")
    found = analyse()
    assert "app/node_modules/lib/node_modules" not in found  # covered by the green parent
    assert found["orphan/node_modules"].band is Band.ORANGE
    assert "orphan/node_modules/lib/node_modules" in found  # parent is not green


def test_sizes_and_numbering(root: Path, analyse: Callable[..., dict[str, Finding]]) -> None:
    touch(root / "big" / "__pycache__" / "a.pyc", b"x" * 50_000)
    touch(root / "small" / "__pycache__" / "a.pyc", b"x")
    touch(root / "orphan" / "node_modules" / "x.js")
    findings = sorted(analyse().values(), key=lambda f: f.number)
    assert [f.number for f in findings] == [1, 2, 3]
    assert findings[0].rel_path_display == "big/__pycache__"  # green, largest first
    assert findings[-1].band is Band.ORANGE
    assert findings[0].allocated_size is not None
    assert findings[0].allocated_size > findings[1].allocated_size  # type: ignore[operator]
    assert findings[0].files == 1


@non_root_only
def test_unscanned_target_has_unknown_size(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    touch(root / "app" / "package.json", b"{}")
    touch(root / "app" / "node_modules" / "x.js")
    (root / "app" / "node_modules").chmod(0o000)
    try:
        found = analyse()
    finally:
        (root / "app" / "node_modules").chmod(0o700)
    assert found["app/node_modules"].allocated_size is None


def test_non_utf8_path_is_detected_but_not_displayable(
    root: Path, analyse: Callable[..., dict[str, Finding]]
) -> None:
    parent = os.fsencode(root) + b"/bad-\xff"
    try:
        os.mkdir(parent)
    except OSError as exc:
        pytest.skip(f"filesystem refuses non-UTF-8 names: {exc}")
    os.mkdir(parent + b"/__pycache__")
    [finding] = analyse().values()
    assert finding.rule.id == "python-pycache"
    assert not finding.displayable


def test_detection_is_read_only_and_deterministic(
    root: Path, analyse: Callable[..., dict[str, Finding]], conn: sqlite3.Connection
) -> None:
    touch(root / "app" / "package.json", b"{}")
    touch(root / "app" / "node_modules" / "x.js")
    first = analyse()
    changes = conn.total_changes
    second = find_parasites(conn, 1, RULES, min_confidence=0)
    assert conn.total_changes == changes  # analysing writes nothing
    assert [f.rel_path for f in second] == [f.rel_path for f in first.values()]
