"""Searching a real scanned tree."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db.connection import connect_memory
from treeaudit.filters import search as search_module
from treeaudit.filters import spec as spec_module
from treeaudit.filters.search import SearchResult, Sort, search
from treeaudit.filters.spec import EntryKindFilter, FilterSpec, compile_filter
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.runner import ScanRequest, run_scan


def touch(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def scanned(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> int:
    root = tmp_path / "root"
    touch(root / "photos" / "IMG_0001.JPG", b"x" * 3000)
    touch(root / "photos" / "IMG_0002.jpg", b"x" * 1000)
    touch(root / "photos" / ".thumb.jpg", b"x" * 10)
    touch(root / "src" / "app.py", b"print()")
    touch(root / "src" / "pkg" / "mod.pyi", b"")
    touch(root / "src" / ".venv" / "lib" / "site.py", b"")
    touch(root / "notes" / unicodedata.normalize("NFC", "café.txt"), b"1")
    touch(root / "notes" / unicodedata.normalize("NFD", "café.txt"), b"2")
    touch(root / "notes" / "ÉTÉ.txt", b"3")
    touch(root / "big" / "video.mp4", b"v" * 50_000)
    os.link(root / "big" / "video.mp4", root / "big" / "same-video.mp4")
    (root / "links").mkdir()
    (root / "links" / "to-photos").symlink_to(root / "photos")
    try:
        fd = os.open(os.fsencode(root / "notes") + b"/bad-\xff.txt", os.O_WRONLY | os.O_CREAT)
        os.close(fd)
    except OSError:
        pass  # filesystem without raw-byte names: the related assertion is skipped
    outcome = run_scan(
        conn,
        ScanRequest(root=RootConfig(id="r", path=str(root), label="r")),
        ctx=ValidationContext(mounts=data_disk_table(str(tmp_path))),
        cancel=threading.Event(),
    )
    return outcome.scan_id


def run(conn: sqlite3.Connection, scan_id: int, **kwargs: object) -> SearchResult:
    options = {k: kwargs.pop(k) for k in ("limit", "sort", "time_budget", "cancel") if k in kwargs}
    return search(conn, scan_id, compile_filter(FilterSpec(**kwargs)), **options)  # type: ignore[arg-type]


def names(result: SearchResult) -> set[str]:
    return {m.rel_path_display for m in result.examples}


def test_extension_is_case_insensitive(conn: sqlite3.Connection, scanned: int) -> None:
    result = run(conn, scanned, extensions=("jpg",), limit=10)
    assert names(result) == {"photos/IMG_0001.JPG", "photos/IMG_0002.jpg", "photos/.thumb.jpg"}
    assert result.files == 3 and result.dirs == 0


def test_glob_case_and_hidden_files(conn: sqlite3.Connection, scanned: int) -> None:
    assert names(run(conn, scanned, name_glob="IMG_*.jpg")) == {"photos/IMG_0002.jpg"}
    insensitive = run(conn, scanned, name_glob="IMG_*.jpg", ignore_case=True)
    assert names(insensitive) == {"photos/IMG_0001.JPG", "photos/IMG_0002.jpg"}
    assert "photos/.thumb.jpg" in names(run(conn, scanned, name_glob="*.jpg"))


def test_path_glob_and_regex(conn: sqlite3.Connection, scanned: int) -> None:
    assert names(run(conn, scanned, path_glob="src/**/*.py*", limit=10)) == {
        "src/app.py",
        "src/pkg/mod.pyi",
        "src/.venv/lib/site.py",
    }
    assert names(run(conn, scanned, path_glob="src/*.py")) == {"src/app.py"}
    venv = run(conn, scanned, path_regex=r"(^|/)\.venv(/|$)", limit=10)
    assert names(venv) == {"src/.venv", "src/.venv/lib", "src/.venv/lib/site.py"}
    assert venv.dirs == 2


def test_full_match_versus_partial(conn: sqlite3.Connection, scanned: int) -> None:
    assert run(conn, scanned, name_regex="app").total == 1
    assert run(conn, scanned, name_regex="app", full_match=True).total == 0
    assert run(conn, scanned, name_regex=r"app\.py", full_match=True).total == 1


def test_unicode_without_normalisation(conn: sqlite3.Connection, scanned: int) -> None:
    nfc = unicodedata.normalize("NFC", "café.txt")
    nfd = unicodedata.normalize("NFD", "café.txt")
    assert run(conn, scanned, name_exact=nfc).total == 1
    assert run(conn, scanned, name_exact=nfd).total == 1
    assert run(conn, scanned, name_regex="^été", ignore_case=True).total == 1


def test_no_result(conn: sqlite3.Connection, scanned: int) -> None:
    result = run(conn, scanned, name_glob="*.does-not-exist")
    assert (result.total, result.examples, result.partial_reason) == (0, [], None)


def test_kinds_and_sizes(conn: sqlite3.Connection, scanned: int) -> None:
    links = run(conn, scanned, kinds=(EntryKindFilter.SYMLINK,))
    assert names(links) == {"links/to-photos"} and links.others == 1
    big = run(conn, scanned, min_size=2000, limit=10)
    assert names(big) == {"photos/IMG_0001.JPG", "big/video.mp4", "big/same-video.mp4"}
    assert big.dirs == 0  # size bounds only concern files
    assert big.files_size == 3000 + 50_000  # the hard link is counted once
    small = run(conn, scanned, max_size=1, kinds=(EntryKindFilter.FILE,), limit=50)
    assert all(m.size is not None and m.size <= 1 for m in small.examples)


def test_directory_sizes(conn: sqlite3.Connection, scanned: int) -> None:
    [photos] = run(conn, scanned, name_exact="photos", kinds=(EntryKindFilter.DIR,)).examples
    assert photos.kind == "dir"
    assert photos.size is not None and photos.size >= 4010


def test_sorting_and_limit(conn: sqlite3.Connection, scanned: int) -> None:
    by_size = run(conn, scanned, kinds=(EntryKindFilter.FILE,), limit=2)
    assert by_size.examples[0].rel_path_display.startswith("big/")
    assert by_size.total > 2 and len(by_size.examples) == 2
    by_path = run(conn, scanned, kinds=(EntryKindFilter.FILE,), sort=Sort.PATH, limit=3)
    paths = [m.rel_path for m in by_path.examples]
    assert paths == sorted(paths)


def test_invalid_utf8_names_are_counted(conn: sqlite3.Connection, scanned: int) -> None:
    result = run(conn, scanned, name_glob="bad-*")
    if result.total == 0:
        pytest.skip("filesystem refused the raw-byte name")
    assert result.not_displayable == 1
    assert names(result) == {"notes/bad-\\xff.txt"}


def test_time_budget_gives_a_partial_result(
    conn: sqlite3.Connection, scanned: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_CHECK_EVERY", 1)
    result = run(conn, scanned, time_budget=-1.0)
    assert result.partial_reason is not None and "budget" in result.partial_reason


def test_cancellation(
    conn: sqlite3.Connection, scanned: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_CHECK_EVERY", 1)
    cancel = threading.Event()
    cancel.set()
    assert run(conn, scanned, cancel=cancel).partial_reason == "recherche annulée"


def test_patterns_are_compiled_once_and_search_writes_nothing(
    conn: sqlite3.Connection, scanned: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled = compile_filter(FilterSpec(name_regex=r"\.py$", path_glob="**"))
    calls: list[str] = []
    real_compile = re.compile

    def counting(pattern: str, flags: int = 0) -> re.Pattern[str]:
        calls.append(pattern)
        return real_compile(pattern, flags)

    monkeypatch.setattr(spec_module.re, "compile", counting)
    changes = conn.total_changes
    result = search(conn, scanned, compiled)
    assert result.total >= 2
    assert calls == []  # nothing compiled while iterating over the entries
    assert conn.total_changes == changes
