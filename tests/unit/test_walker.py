"""Walker tests on real temporary trees."""

from __future__ import annotations

import errno
import os
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from treeaudit.scanner import walker
from treeaudit.scanner.models import (
    DirDone,
    EntryKind,
    EntryRecord,
    ErrorOperation,
    ErrorRecord,
    ExclusionRecord,
    WalkEvent,
)
from treeaudit.scanner.walker import SubpathError, WalkOptions, parse_subpath, walk

# open(O_DIRECTORY | O_NOFOLLOW) on a symlink fails with ENOTDIR on Linux
# (O_DIRECTORY is checked first), ELOOP without O_DIRECTORY.
SYMLINK_REFUSED = (errno.ELOOP, errno.ENOTDIR)

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")


def run(root: Path, subpath: tuple[str, ...] = (), **kwargs: object) -> list[WalkEvent]:
    options = WalkOptions(root_path=str(root), **kwargs)  # type: ignore[arg-type]
    return list(walk(options, subpath))


def entries(events: list[WalkEvent]) -> dict[bytes, EntryRecord]:
    return {e.rel_path: e for e in events if isinstance(e, EntryRecord)}


def errors(events: list[WalkEvent]) -> list[ErrorRecord]:
    return [e for e in events if isinstance(e, ErrorRecord)]


def start_totals(events: list[WalkEvent]) -> DirDone:
    done = events[-1]
    assert isinstance(done, DirDone)
    assert done.entry_id == 1
    return done


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "top.txt").write_bytes(b"x" * 10)
    (root / "a" / "mid.py").write_bytes(b"y" * 20)
    (root / "a" / "b" / "deep.TXT").write_bytes(b"z" * 30)
    return root


# --- basic inventory ------------------------------------------------------------------


def test_empty_root(tmp_path: Path) -> None:
    events = run(tmp_path)
    assert [type(e) for e in events] == [EntryRecord, DirDone]
    start = events[0]
    assert isinstance(start, EntryRecord)
    assert (start.entry_id, start.parent_id, start.depth, start.rel_path) == (1, None, 0, b"")
    assert start.kind is EntryKind.DIR
    totals = start_totals(events).totals
    assert (totals.files, totals.dirs, totals.logical_size, totals.errors) == (0, 0, 0, 0)


def test_inventory_of_a_tree(tree: Path) -> None:
    events = run(tree)
    found = entries(events)
    assert set(found) == {b"", b"a", b"a/b", b"empty", b"top.txt", b"a/mid.py", b"a/b/deep.TXT"}
    assert found[b"a/b/deep.TXT"].depth == 3
    assert found[b"a/b/deep.TXT"].parent_id == found[b"a/b"].entry_id
    assert found[b"a/b/deep.TXT"].size == 30
    assert found[b"top.txt"].kind is EntryKind.FILE
    assert found[b"a"].kind is EntryKind.DIR
    totals = start_totals(events).totals
    assert (totals.files, totals.dirs, totals.logical_size) == (3, 3, 60)
    assert errors(events) == []


def test_parents_are_emitted_before_children(tree: Path) -> None:
    seen: set[int] = set()
    for event in run(tree):
        if isinstance(event, EntryRecord):
            assert event.parent_id is None or event.parent_id in seen
            seen.add(event.entry_id)


def test_dir_summaries_aggregate_subtrees(tree: Path) -> None:
    events = run(tree)
    found = entries(events)
    summaries = {e.entry_id: e.totals for e in events if isinstance(e, DirDone)}
    a = summaries[found[b"a"].entry_id]
    assert (a.files, a.dirs, a.logical_size) == (2, 1, 50)
    assert summaries[found[b"empty"].entry_id].files == 0


def test_hard_links_are_counted_once(tmp_path: Path) -> None:
    (tmp_path / "original").write_bytes(b"h" * 1000)
    os.link(tmp_path / "original", tmp_path / "second-name")
    events = run(tmp_path)
    totals = start_totals(events).totals
    assert totals.files == 2  # two entries
    assert totals.logical_size == 1000  # one inode
    assert entries(events)[b"second-name"].nlink == 2


def test_sparse_file_sizes(tmp_path: Path) -> None:
    with (tmp_path / "sparse").open("wb") as handle:
        handle.truncate(10 * 1024 * 1024)
    record = entries(run(tmp_path))[b"sparse"]
    assert record.size == 10 * 1024 * 1024
    if record.allocated_size >= record.size:
        pytest.skip("this filesystem does not support sparse files")
    assert record.allocated_size < record.size


# --- names ----------------------------------------------------------------------------


def test_unusual_names(tmp_path: Path) -> None:
    names = ["with space", "new\nline", ".hidden", "émoji 📁", "tab\there", "-dash"]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    found = entries(run(tmp_path))
    for name in names:
        assert name.encode() in found


def test_nfc_and_nfd_names_are_distinct(tmp_path: Path) -> None:
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    (tmp_path / nfc).write_bytes(b"1")
    (tmp_path / nfd).write_bytes(b"2")
    found = entries(run(tmp_path))
    assert nfc.encode() in found
    assert nfd.encode() in found


def test_invalid_utf8_name_is_kept_as_bytes(tmp_path: Path) -> None:
    raw = b"bad-\xff\xfe-name.txt"
    try:
        fd = os.open(os.path.join(os.fsencode(tmp_path), raw), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:  # some filesystems refuse non-UTF-8 names
        pytest.skip(f"filesystem refuses non-UTF-8 names: {exc}")
    os.close(fd)
    found = entries(run(tmp_path))
    assert raw in found
    assert found[raw].name == raw


# --- links and special files ----------------------------------------------------------


def test_symlinks_are_recorded_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    (root / "to-outside").symlink_to(outside)
    (root / "to-etc").symlink_to("/etc")
    (root / "broken").symlink_to(root / "nowhere")
    (root / "loop").symlink_to(root)
    found = entries(run(root))
    for name in (b"to-outside", b"to-etc", b"broken", b"loop"):
        assert found[name].kind is EntryKind.SYMLINK
    assert found[b"to-etc"].link_target == b"/etc"
    # Nothing below any link.
    assert all(b"/" not in path for path in found)


def test_fifo_is_listed_not_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    events = run(tmp_path)  # would hang if the fifo were opened
    assert entries(events)[b"pipe"].kind is EntryKind.OTHER
    assert start_totals(events).totals.others == 1


# --- errors, limits, exclusions -------------------------------------------------------


@non_root_only
def test_unreadable_directory(tmp_path: Path) -> None:
    (tmp_path / "locked" / "inside").mkdir(parents=True)
    (tmp_path / "locked").chmod(0o000)
    try:
        events = run(tmp_path)
    finally:
        (tmp_path / "locked").chmod(0o700)
    [error] = errors(events)
    assert (error.rel_path, error.operation, error.errno) == (
        b"locked",
        ErrorOperation.OPEN_DIR,
        errno.EACCES,
    )
    assert b"locked" in entries(events)  # the directory itself is listed
    assert start_totals(events).totals.errors == 1


@non_root_only
def test_listable_but_not_traversable_directory(tmp_path: Path) -> None:
    (tmp_path / "noexec").mkdir()
    (tmp_path / "noexec" / "file").write_bytes(b"x")
    (tmp_path / "noexec").chmod(0o400)  # read (list names) without search (stat)
    try:
        events = run(tmp_path)
    finally:
        (tmp_path / "noexec").chmod(0o700)
    # Names can be listed, but stat() of the entries is denied.
    [error] = errors(events)
    assert (error.rel_path, error.operation, error.errno) == (
        b"noexec/file",
        ErrorOperation.STAT_ENTRY,
        errno.EACCES,
    )
    assert b"noexec/file" not in entries(events)


def test_max_depth(tmp_path: Path) -> None:
    (tmp_path / "1" / "2" / "3" / "4").mkdir(parents=True)
    events = run(tmp_path, max_depth=2)
    found = entries(events)
    assert b"1/2" in found and b"1/2/3" in found  # listed as entries of depth 2
    assert b"1/2/3/4" not in found  # 1/2/3 was not opened
    [error] = errors(events)
    assert (error.rel_path, error.operation) == (b"1/2/3", ErrorOperation.MAX_DEPTH)


def test_mount_points_are_excluded(tmp_path: Path) -> None:
    (tmp_path / "disk" / "data").mkdir(parents=True)
    events = run(tmp_path, mount_points=frozenset({str(tmp_path / "disk")}))
    exclusions = [e for e in events if isinstance(e, ExclusionRecord)]
    assert [x.rel_path for x in exclusions] == [b"disk"]
    assert b"disk/data" not in entries(events)
    assert errors(events) == []


def test_mount_points_crossed_when_allowed(tmp_path: Path) -> None:
    (tmp_path / "disk" / "data").mkdir(parents=True)
    events = run(tmp_path, mount_points=frozenset({str(tmp_path / "disk")}), cross_mounts=True)
    assert b"disk/data" in entries(events)


# --- races ------------------------------------------------------------------------------


def test_directory_swapped_for_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "victim").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "secret.txt").parent.mkdir()
    (outside / "secret.txt").write_bytes(b"s")
    real_open = walker._open_dir

    def swapping_open(name: str, dir_fd: int | None) -> int:
        if name == "victim":
            (root / "victim").rename(root / "moved")
            (root / "victim").symlink_to(outside)
        return real_open(name, dir_fd)

    monkeypatch.setattr(walker, "_open_dir", swapping_open)
    events = run(root)
    [error] = errors(events)
    assert (error.rel_path, error.operation) == (b"victim", ErrorOperation.OPEN_DIR)
    assert error.errno in SYMLINK_REFUSED
    assert not any(path.startswith(b"victim/") for path in entries(events))


def test_directory_swapped_for_another_directory_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "victim").mkdir(parents=True)
    (tmp_path / "other" / "intruder.txt").parent.mkdir()
    (tmp_path / "other" / "intruder.txt").write_bytes(b"i")
    real_open = walker._open_dir

    def swapping_open(name: str, dir_fd: int | None) -> int:
        if name == "victim":
            (root / "victim").rename(root / "moved")
            (tmp_path / "other").rename(root / "victim")
        return real_open(name, dir_fd)

    monkeypatch.setattr(walker, "_open_dir", swapping_open)
    events = run(root)
    [error] = errors(events)
    assert (error.rel_path, error.operation) == (b"victim", ErrorOperation.CHANGED_DURING_SCAN)
    assert b"victim/intruder.txt" not in entries(events)


def test_entry_vanishing_before_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ghost").write_bytes(b"g")
    (tmp_path / "stays").write_bytes(b"s")
    real_lstat = walker._lstat_at

    def vanishing(name: str, dir_fd: int) -> os.stat_result:
        if name == "ghost":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return real_lstat(name, dir_fd)

    monkeypatch.setattr(walker, "_lstat_at", vanishing)
    events = run(tmp_path)
    [error] = errors(events)
    assert (error.rel_path, error.operation) == (b"ghost", ErrorOperation.STAT_ENTRY)
    assert b"stays" in entries(events)


def test_root_swapped_since_validation(tmp_path: Path) -> None:
    events = run(tmp_path, expected_root=(0, 0))
    [error] = errors(events)
    assert error.operation is ErrorOperation.CHANGED_DURING_SCAN
    assert entries(events) == {}


def test_missing_root(tmp_path: Path) -> None:
    events = run(tmp_path / "missing")
    [error] = errors(events)
    assert (error.operation, error.errno) == (ErrorOperation.OPEN_ROOT, errno.ENOENT)


def test_root_given_as_symlink_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    [error] = errors(run(tmp_path / "link"))
    assert error.operation is ErrorOperation.OPEN_ROOT
    assert error.errno in SYMLINK_REFUSED


# --- sub-folder (D-021) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "subpath", ["/etc", "../x", "a/../b", "a//b", "./a", "a/.", "a/", "a\x00b"]
)
def test_unsafe_subpaths(subpath: str) -> None:
    with pytest.raises(SubpathError):
        parse_subpath(subpath)


@pytest.mark.parametrize(("subpath", "parts"), [("", ()), (".", ()), ("a/b c", ("a", "b c"))])
def test_safe_subpaths(subpath: str, parts: tuple[str, ...]) -> None:
    assert parse_subpath(subpath) == parts


def test_walk_a_subfolder(tree: Path) -> None:
    events = run(tree, ("a", "b"))
    found = entries(events)
    assert set(found) == {b"a/b", b"a/b/deep.TXT"}  # paths stay relative to the root
    assert found[b"a/b"].depth == 0
    assert found[b"a/b"].name == b"b"


def test_subfolder_through_symlink_is_refused(tree: Path, tmp_path: Path) -> None:
    (tree / "shortcut").symlink_to(tmp_path)
    [error] = errors(run(tree, ("shortcut",)))
    assert error.operation is ErrorOperation.OPEN_SUBPATH
    assert error.errno in SYMLINK_REFUSED


def test_subfolder_that_is_a_file(tree: Path) -> None:
    [error] = errors(run(tree, ("top.txt",)))
    assert (error.operation, error.errno) == (ErrorOperation.OPEN_SUBPATH, errno.ENOTDIR)


# --- resources --------------------------------------------------------------------------


def test_no_descriptor_leak_after_full_walk(tree: Path) -> None:
    before = open_fds()
    run(tree)
    assert open_fds() == before


def test_no_descriptor_leak_when_stopped_early(tree: Path) -> None:
    before = open_fds()
    events: Iterator[WalkEvent] = walk(WalkOptions(root_path=str(tree)))
    for event in events:
        if isinstance(event, EntryRecord) and event.rel_path == b"a/b":
            break
    events.close()  # type: ignore[attr-defined]
    assert open_fds() == before


def test_deep_tree_uses_bounded_descriptors(tmp_path: Path) -> None:
    path = tmp_path
    for _ in range(100):
        path = path / "d"
    path.mkdir(parents=True)
    events = run(tmp_path)
    assert len(entries(events)) == 101
    assert errors(events) == []


def test_entries_are_examined_in_inode_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(50):
        (tmp_path / f"f{index:02d}").write_bytes(b"")
    seen: list[int] = []
    real_lstat = walker._lstat_at

    def recording(name: str, dir_fd: int) -> os.stat_result:
        st = real_lstat(name, dir_fd)
        seen.append(st.st_ino)
        return st

    monkeypatch.setattr(walker, "_lstat_at", recording)
    run(tmp_path)
    assert seen == sorted(seen)


def test_inode_batches_bound_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(walker, "STAT_BATCH", 7)
    for index in range(30):
        (tmp_path / f"f{index:02d}").write_bytes(b"")
    events = run(tmp_path)
    assert len(entries(events)) == 31  # nothing lost across batches
