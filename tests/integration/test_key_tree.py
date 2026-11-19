"""The USB key generator's sheet must be the truth TreeAudit is compared with.

The generator counts what it writes; these tests scan the same folder with
TreeAudit and check that both agree, also when the file system refuses things.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.make_key_tree import KeyTree, generate, main
from treeaudit.core.config import RootConfig
from treeaudit.db import scans
from treeaudit.db.connection import connect_memory
from treeaudit.duplicates.groups import load_groups
from treeaudit.duplicates.runner import DupOptions, run_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.runner import ScanRequest, run_scan


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def ctx(tmp_path: Path, data_disk_table: Callable[..., MountTable]) -> ValidationContext:
    return ValidationContext(mounts=data_disk_table(str(tmp_path)), data_dir=str(tmp_path / "data"))


def audit(conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> dict[str, Any]:
    """What TreeAudit finds in ``root``, in the sheet's terms."""
    config = RootConfig(id="key", path=str(root), label="key")
    outcome = run_scan(conn, ScanRequest(root=config), ctx=ctx, cancel=threading.Event())
    row = scans.get_scan(conn, outcome.scan_id)
    assert row is not None
    assert (row["status"], row["errors"]) == ("complete", 0)
    dupes = run_duplicates(
        conn, outcome.scan_id, config, ctx=ctx, options=DupOptions(), cancel=threading.Event()
    )
    report = load_groups(conn, dupes.run_id)
    return {
        "totals": {
            key: row[key]
            for key in ("files", "dirs", "symlinks", "others", "entries", "logical_size")
        }
        | {"allocated_size": row["allocated_size"]},
        "hardlinked_inodes": report.hardlinked_inodes,
        "duplicate_groups": sorted(
            sorted(c.rel_path_display for c in g.copies) for g in report.groups
        ),
    }


def sheet_part(sheet: dict[str, Any]) -> dict[str, Any]:
    return {key: sheet[key] for key in ("totals", "hardlinked_inodes", "duplicate_groups")}


def test_the_sheet_matches_what_treeaudit_finds(
    tmp_path: Path, conn: sqlite3.Connection, ctx: ValidationContext
) -> None:
    target = tmp_path / "key"
    target.mkdir()
    sheet = generate(target, small_files=300, big_mb=1)
    assert sheet_part(sheet) == audit(conn, target, ctx)
    # The answer is not trivial: groups of 3 and of 2, hard links, every kind.
    sizes = sorted(len(g) for g in sheet["duplicate_groups"])
    assert sizes[-1] == 3 and sizes.count(2) == 1 + 10 + 8  # big pair, album, holidays
    assert sheet["hardlinked_inodes"] == 2
    totals = sheet["totals"]
    assert totals["symlinks"] == 4 and totals["others"] == 1
    # Only names the Linux file system cannot hold are refused here.
    assert {r["error"] for r in sheet["refused"]} <= {"ENAMETOOLONG"}


def test_what_the_file_system_refuses_is_noted_and_still_counted_right(
    tmp_path: Path,
    conn: sqlite3.Connection,
    ctx: ValidationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like exFAT: no links, no fifo, no hard link. The generator goes on and
    its totals still match the scan."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "refused")

    for name in ("symlink", "link", "mkfifo"):
        monkeypatch.setattr(os, name, refuse)
    target = tmp_path / "key"
    sheet = generate(target, small_files=50, big_mb=1)
    monkeypatch.undo()
    refused = {(r["what"], r["error"]) for r in sheet["refused"]}
    assert {("symbolic link", "EPERM"), ("hard link", "EPERM"), ("fifo", "EPERM")} <= refused
    assert sheet["totals"]["symlinks"] == sheet["totals"]["others"] == 0
    assert sheet["hardlinked_inodes"] == 0
    assert sheet_part(sheet) == audit(conn, target, ctx)


def test_a_name_differing_only_in_case_is_noted_when_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a file system that ignores case, the second name already exists."""
    real_open = os.open

    def case_blind_open(path: bytes, flags: int, mode: int = 0o777) -> int:
        if path.endswith(b"/photo.jpg"):
            raise OSError(errno.EEXIST, "exists")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", case_blind_open)
    sheet = generate(tmp_path / "key", small_files=0, big_mb=1)
    assert {"what": "name", "path": "noms/photo.jpg", "error": "EEXIST"} in sheet["refused"]


def test_the_allocated_size_counts_the_root_folder_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In /tmp (tmpfs) a folder occupies no block, which hid this: every entry,
    the root included, is given one block here."""
    tree = KeyTree(tmp_path)
    assert tree.mkdir("a") and tree.file("a/b.txt", b"x")
    monkeypatch.setattr(
        os, "lstat", lambda path: SimpleNamespace(st_dev=1, st_ino=hash(path), st_blocks=1)
    )
    assert tree.allocated_size() == 3 * 512


def test_the_command_writes_the_sheet_off_the_tree(tmp_path: Path) -> None:
    target, sheet = tmp_path / "key", tmp_path / "sheet.json"
    assert (
        main([str(target), "--manifest", str(sheet), "--small-files", "10", "--big-mb", "1"]) == 0
    )
    written = json.loads(sheet.read_text(encoding="utf-8"))
    assert written["totals"]["files"] > 10
    assert written["parameters"] == {"small_files": 10, "big_mb": 1, "seed": 20261119}


@pytest.mark.parametrize("case", ["non-empty target", "sheet inside", "sheet exists"])
def test_the_command_refuses_to_overwrite_anything(tmp_path: Path, case: str) -> None:
    target, sheet = tmp_path / "key", tmp_path / "sheet.json"
    target.mkdir()
    if case == "non-empty target":
        (target / "kept.txt").write_bytes(b"kept")
    elif case == "sheet inside":
        sheet = target / "sheet.json"
    else:
        sheet.write_bytes(b"kept")
    assert main([str(target), "--manifest", str(sheet), "--big-mb", "1"]) == 2
    if case == "non-empty target":
        assert os.listdir(target) == ["kept.txt"]
    elif case == "sheet inside":
        assert os.listdir(target) == []
    else:
        assert sheet.read_bytes() == b"kept"
