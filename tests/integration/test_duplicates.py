"""Duplicate analysis on real temporary trees: scan, then read and compare."""

from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db import dupes as dupes_db
from treeaudit.db.connection import connect, connect_memory, database_path
from treeaudit.duplicates import hashing, runner
from treeaudit.duplicates.groups import GroupReport, load_groups
from treeaudit.duplicates.report import (
    SelectionError,
    override_keep,
    parse_groups,
    render_command,
)
from treeaudit.duplicates.runner import DuplicateAnalysisError, DupOptions, run_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.models import ScanStatus
from treeaudit.scanner.runner import ScanRequest, run_scan
from treeaudit.shellcmd import Action

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")

KIB = 1024
SECRET = b"TOP-SECRET-DUPLICATED-CONTENT-" * 100


def touch(path: Path, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def ctx(tmp_path: Path, data_disk_table: Callable[..., MountTable]) -> ValidationContext:
    return ValidationContext(mounts=data_disk_table(str(tmp_path)))


class Harness:
    def __init__(self, conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> None:
        self.conn = conn
        self.root_config = RootConfig(id="r", path=str(root), label="r")
        self.ctx = ctx
        self.scan_id: int | None = None

    def scan(self) -> int:
        outcome = run_scan(
            self.conn, ScanRequest(root=self.root_config), ctx=self.ctx, cancel=threading.Event()
        )
        self.scan_id = outcome.scan_id
        return outcome.scan_id

    def analyse(
        self, options: DupOptions | None = None, cancel: threading.Event | None = None
    ) -> runner.DupOutcome:
        if self.scan_id is None:
            self.scan()
        assert self.scan_id is not None
        return run_duplicates(
            self.conn,
            self.scan_id,
            self.root_config,
            ctx=self.ctx,
            options=options or DupOptions(),
            cancel=cancel or threading.Event(),
        )

    def groups(self, options: DupOptions | None = None) -> tuple[runner.DupOutcome, GroupReport]:
        outcome = self.analyse(options)
        return outcome, load_groups(self.conn, outcome.run_id)


@pytest.fixture
def harness(conn: sqlite3.Connection, root: Path, ctx: ValidationContext) -> Harness:
    return Harness(conn, root, ctx)


def paths(report: GroupReport) -> list[list[str]]:
    return [[c.rel_path_display for c in g.copies] for g in report.groups]


# --- finding duplicates -------------------------------------------------------------------


def test_identical_files_with_different_names(root: Path, harness: Harness) -> None:
    content = os.urandom(300 * KIB)
    touch(root / "videos" / "clip.mp4", content)
    touch(root / "backup" / "old" / "clip.mp4", content)
    touch(root / "Downloads" / "clip (1).mp4", content)
    touch(root / "other.bin", os.urandom(300 * KIB))
    outcome, report = harness.groups()
    assert outcome.status is ScanStatus.COMPLETE
    [group] = report.groups
    assert len(group.copies) == 3
    assert [c.rel_path_display for c in group.keep] == ["videos/clip.mp4"]
    assert group.recoverable == 2 * 300 * KIB
    assert len(group.removable) == 2
    assert group.digest == hashlib.sha256(content).digest()


def test_same_size_but_different_content_is_not_a_duplicate(root: Path, harness: Harness) -> None:
    # Fixed first and last bytes, so that "A"/"B" edits always make a difference.
    base = b"\x00" + os.urandom(512 * KIB - 2) + b"\x00"
    touch(root / "head" / "a", b"A" + base[1:])
    touch(root / "head" / "b", b"B" + base[1:])
    touch(root / "tail" / "a", base[:-1] + b"A")
    touch(root / "tail" / "b", base[:-1] + b"B")
    middle = bytearray(base)
    middle[256 * KIB] ^= 0xFF
    touch(root / "middle" / "a", base)
    touch(root / "middle" / "b", bytes(middle))
    outcome, report = harness.groups()
    assert report.groups == []
    # Head and tail pairs are told apart by the partial digest; only the
    # "middle" pair needs a full read to be told apart.
    assert outcome.files_hashed == 6 + 2


def test_small_files_use_one_read(root: Path, harness: Harness) -> None:
    for name in "abc":
        touch(root / name, b"z" * 2 * KIB)
    outcome, report = harness.groups()
    assert outcome.files_hashed == 3  # partial digest covers small files entirely
    assert outcome.bytes_read == 3 * 2 * KIB
    assert len(report.groups) == 1


def test_hard_links_are_not_duplicates(root: Path, harness: Harness) -> None:
    touch(root / "original", os.urandom(10 * KIB))
    os.link(root / "original", root / "alias")
    outcome, report = harness.groups()
    assert report.groups == []
    assert report.hardlinked_inodes == 1
    assert outcome.candidates == 0  # one inode: nothing to compare


def test_hard_link_copies_are_shown_and_freed_once(root: Path, harness: Harness) -> None:
    content = os.urandom(10 * KIB)
    touch(root / "a" / "keep.dat", content)
    touch(root / "copy" / "x.dat", content)
    os.link(root / "copy" / "x.dat", root / "copy" / "y.dat")
    _, report = harness.groups()
    [group] = report.groups
    assert len(group.copies) == 3
    assert group.inodes == 2
    assert group.recoverable == 10 * KIB


# --- micro-cleanup and protections ---------------------------------------------------------


def test_min_size_and_micro_mode(root: Path, harness: Harness) -> None:
    for index in range(5):
        touch(root / f"d{index}" / "note.txt", b"hello\n")
        touch(root / "logs" / f"e{index}.log")
    touch(root / "lonely" / "empty.txt")
    _, main_report = harness.groups()
    assert main_report.groups == []  # below 1 KiB
    _, micro = harness.groups(DupOptions.for_micro())
    notes, empties = sorted(micro.groups, key=lambda g: g.size, reverse=True)
    assert len(notes.removable) == 4
    assert empties.is_empty_files and empties.zone == "logs"
    assert len(empties.removable) == 5  # nothing to keep in empty files
    # A zone with a single empty file is not a group.
    assert all(g.zone != "lonely" for g in micro.groups)


def test_empty_files_are_never_selected_in_bulk(root: Path, harness: Harness) -> None:
    for index in range(3):
        touch(root / f"d{index}" / "note.txt", b"hello\n")
        touch(root / "logs" / f"e{index}.log")
    _, report = harness.groups(DupOptions.for_micro())
    selected = parse_groups("all", report.groups)
    assert selected and not any(g.is_empty_files for g in selected)
    empty_group = next(g for g in report.groups if g.is_empty_files)
    assert parse_groups(f"G{empty_group.number}", report.groups) == [empty_group]


def test_protected_copies(root: Path, harness: Harness) -> None:
    content = b"same small content"
    touch(root / "repo" / ".git" / "HEAD", b"ref")
    touch(root / "repo" / "data.txt", content)
    touch(root / "pkg" / "__init__.py", content)
    touch(root / "Steam" / "steamapps" / "common" / "Game" / "data.txt", content)
    touch(root / "docs" / "data.txt", content)
    touch(root / "docs" / "copy of data.txt", content)
    _, report = harness.groups(DupOptions.for_micro())
    [group] = report.groups
    protected = {c.rel_path_display: c.protection for c in group.copies if c.protection}
    assert set(protected) == {
        "repo/data.txt",
        "pkg/__init__.py",
        "Steam/steamapps/common/Game/data.txt",
    }
    assert "dépôt git" in (protected["repo/data.txt"] or "")
    assert "steamapps" in (protected["Steam/steamapps/common/Game/data.txt"] or "")
    # Protected copies are kept, plus one free copy: only the "copy of" goes.
    assert [c.rel_path_display for c in group.removable] == ["docs/copy of data.txt"]


def test_a_single_free_copy_is_never_proposed(root: Path, harness: Harness) -> None:
    content = os.urandom(4 * KIB)
    touch(root / "Steam" / "steamapps" / "common" / "A" / "lib.dll", content)
    touch(root / "Steam" / "steamapps" / "common" / "B" / "lib.dll", content)
    touch(root / "my-project" / "bin" / "lib.dll", content)
    _, report = harness.groups()
    assert report.groups == []  # the user's only copy is kept
    assert report.protected_copies == 2


# --- safety: files changed since the scan ----------------------------------------------------


def test_file_modified_after_scan_is_excluded(root: Path, harness: Harness) -> None:
    content = os.urandom(8 * KIB)
    touch(root / "a", content)
    target = touch(root / "b", content)
    touch(root / "c", content)
    harness.scan()
    target.write_bytes(os.urandom(8 * KIB))  # same size, new content and mtime
    outcome, report = harness.groups()
    assert outcome.status is ScanStatus.INCOMPLETE
    [group] = report.groups
    assert sorted(c.rel_path_display for c in group.copies) == ["a", "c"]
    [error] = dupes_db.list_errors(harness.conn, outcome.run_id)
    assert (error["rel_path_display"], error["operation"]) == ("b", "changed")


def test_file_replaced_by_symlink_or_fifo(root: Path, harness: Harness, tmp_path: Path) -> None:
    content = os.urandom(8 * KIB)
    for name in ("a", "link", "fifo", "z"):
        touch(root / name, content)
    touch(tmp_path / "outside", content)
    harness.scan()
    (root / "link").unlink()
    (root / "link").symlink_to(tmp_path / "outside")
    (root / "fifo").unlink()
    os.mkfifo(root / "fifo")  # would block a reader without O_NONBLOCK
    outcome, report = harness.groups()
    errors = {
        e["rel_path_display"]: e["operation"]
        for e in dupes_db.list_errors(harness.conn, outcome.run_id)
    }
    assert errors == {"link": "open", "fifo": "changed"}
    assert paths(report) == [["a", "z"]]


def test_directory_replaced_by_symlink_is_not_followed(
    root: Path, harness: Harness, tmp_path: Path
) -> None:
    content = os.urandom(8 * KIB)
    touch(root / "dir" / "f", content)
    touch(root / "g", content)
    harness.scan()
    (root / "dir").rename(root / "moved")
    touch(tmp_path / "evil" / "f", content)
    (root / "dir").symlink_to(tmp_path / "evil")
    outcome, report = harness.groups()
    [error] = dupes_db.list_errors(harness.conn, outcome.run_id)
    assert (error["rel_path_display"], error["operation"]) == ("dir/f", "open")
    assert report.groups == []


@non_root_only
def test_unreadable_file_makes_the_analysis_incomplete(root: Path, harness: Harness) -> None:
    content = os.urandom(8 * KIB)
    for name in ("a", "b", "locked"):
        touch(root / name, content)
    (root / "locked").chmod(0o000)
    try:
        outcome, report = harness.groups()
    finally:
        (root / "locked").chmod(0o600)
    assert outcome.status is ScanStatus.INCOMPLETE
    assert paths(report) == [["a", "b"]]


def test_root_replaced_since_the_scan(root: Path, harness: Harness, tmp_path: Path) -> None:
    for name in "ab":
        touch(root / name, b"x" * 2 * KIB)
    harness.scan()
    root.rename(tmp_path / "old-root")
    root.mkdir()
    for name in "ab":
        touch(root / name, b"x" * 2 * KIB)
    outcome = harness.analyse()
    assert outcome.status is ScanStatus.FAILED
    assert "not the scanned directory" in (outcome.failure or "")


def test_scan_of_another_root_is_refused(root: Path, harness: Harness, tmp_path: Path) -> None:
    harness.scan()
    other = RootConfig(id="other", path=str(tmp_path), label="o")
    with pytest.raises(DuplicateAnalysisError):
        run_duplicates(
            harness.conn,
            harness.scan_id or 0,
            other,
            ctx=harness.ctx,
            options=DupOptions(),
            cancel=threading.Event(),
        )


# --- statuses, verification, resources ------------------------------------------------------


def test_cancelled_analysis(root: Path, harness: Harness) -> None:
    for name in "abc":
        touch(root / name, b"y" * 4 * KIB)
    cancel = threading.Event()
    cancel.set()
    assert harness.analyse(cancel=cancel).status is ScanStatus.CANCELLED


def test_internal_failure(root: Path, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in "ab":
        touch(root / name, b"y" * 4 * KIB)

    def broken(fd: int, size: int) -> tuple[bytes, int]:
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(runner, "partial_digest", broken)
    outcome = harness.analyse()
    assert outcome.status is ScanStatus.FAILED
    assert "simulated bug" in (outcome.failure or "")


def test_byte_verification(root: Path, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    content = os.urandom(300 * KIB)
    for name in "abc":
        touch(root / name, content)
    outcome, report = harness.groups(DupOptions(verify_bytes=True))
    assert outcome.status is ScanStatus.COMPLETE
    assert len(report.groups[0].copies) == 3
    assert outcome.bytes_read > 3 * 300 * KIB  # digests, then comparison

    monkeypatch.setattr(runner, "same_content", lambda *a, **k: False)
    outcome, report = harness.groups(DupOptions(verify_bytes=True))
    assert outcome.status is ScanStatus.INCOMPLETE
    assert any(
        "collision" in e["message"] for e in dupes_db.list_errors(harness.conn, outcome.run_id)
    )


def test_descriptors_are_bounded_and_released(root: Path, harness: Harness) -> None:
    content = os.urandom(4 * KIB)
    for index in range(30):
        touch(root / f"d{index % 3}" / f"s{index}" / "deep" / "f.bin", content)
    before = open_fds()
    outcome, report = harness.groups()
    assert open_fds() == before
    assert len(report.groups[0].copies) == 30
    assert outcome.status is ScanStatus.COMPLETE


def test_no_file_content_in_the_index(tmp_path: Path, root: Path, ctx: ValidationContext) -> None:
    touch(root / "a", SECRET)
    touch(root / "b", SECRET)
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    connection = connect(str(data_dir))
    try:
        harness = Harness(connection, root, ctx)
        _, report = harness.groups()
        assert len(report.groups) == 1
    finally:
        connection.close()
    assert database_path(str(data_dir)).exists()
    for item in data_dir.iterdir():
        assert SECRET[:40] not in item.read_bytes(), item.name


def test_interrupted_runs_are_marked_failed(conn: sqlite3.Connection, harness: Harness) -> None:
    scan_id = harness.scan()
    run_id = dupes_db.create_run(conn, scan_id, {})
    assert dupes_db.fail_interrupted_runs(conn) == 1
    row = dupes_db.get_run(conn, run_id)
    assert row is not None and row["status"] == "failed"


# --- hashing primitives -------------------------------------------------------------------------


def test_digests(tmp_path: Path) -> None:
    big = os.urandom(3 * hashing.BLOCK_SIZE + 123)
    path = touch(tmp_path / "big", big)
    fd = os.open(path, os.O_RDONLY)
    try:
        partial, read = hashing.partial_digest(fd, len(big))
        assert read == 2 * hashing.PART_SIZE
        expected = hashlib.sha256(big[: hashing.PART_SIZE] + big[-hashing.PART_SIZE :]).digest()
        assert partial == expected
        seen: list[int] = []
        full = hashing.full_digest(fd, len(big), cancel=threading.Event(), on_read=seen.append)
        assert full == hashlib.sha256(big).digest()
        assert sum(seen) == len(big)
        with pytest.raises(OSError, match="shorter"):
            hashing.full_digest(fd, len(big) + 10, cancel=threading.Event(), on_read=seen.append)
    finally:
        os.close(fd)


# --- groups, selection and commands ----------------------------------------------------------


def test_command_removes_only_unkept_copies(root: Path, harness: Harness) -> None:
    content = os.urandom(4 * KIB)
    touch(root / "photos" / "img.jpg", content)
    touch(root / "Downloads" / "img (1).jpg", content)
    touch(root / "$(touch pwned)" / "img.jpg", content)
    _, report = harness.groups()
    selected = parse_groups("all", report.groups)
    text = render_command(selected, Action.TRASH, root_display="/mnt/d")
    command = text.split("\n\n", 1)[1]
    argv = shlex.split(command.replace(" \\\n  ", " "))
    assert argv[:3] == ["gio", "trash", "--"]
    assert sorted(argv[3:]) == ["/mnt/d/$(touch pwned)/img.jpg", "/mnt/d/Downloads/img (1).jpg"]


def test_keep_override(root: Path, harness: Harness) -> None:
    content = os.urandom(4 * KIB)
    touch(root / "a" / "f", content)
    touch(root / "b" / "f", content)
    _, report = harness.groups()
    [group] = report.groups
    changed = override_keep(group, 2)
    assert [c.rel_path_display for c in changed.keep] == ["b/f"]
    assert [c.rel_path_display for c in changed.removable] == ["a/f"]
    with pytest.raises(SelectionError):
        override_keep(group, 3)


@pytest.mark.parametrize("text", ["", "G9", "x", "3-1"])
def test_invalid_group_selection(text: str, root: Path, harness: Harness) -> None:
    content = os.urandom(4 * KIB)
    touch(root / "a", content)
    touch(root / "b", content)
    _, report = harness.groups()
    with pytest.raises(SelectionError):
        parse_groups(text, report.groups)
