"""Folders that are (almost) the same (D-058), from a real scan and search."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db.connection import connect_memory
from treeaudit.duplicates.runner import DupOptions, run_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.runner import ScanRequest, run_scan
from treeaudit.similar.trees import Comparison, Report, similar_folders

Tree = dict[str, bytes]


def blob(tag: str, size: int = 4000) -> bytes:
    """Content of a given size, told apart by its tag at the start and end."""
    edge = tag.encode().ljust(64, b"_")
    return edge + b"." * (size - 128) + edge


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def analyse(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> Callable[..., Report | None]:
    def run(
        tree: Tree, *, search: bool = True, links: dict[str, str] | None = None, subpath: str = ""
    ) -> Report | None:
        root = tmp_path / "root"
        for path, content in tree.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for link, existing in (links or {}).items():
            (root / link).parent.mkdir(parents=True, exist_ok=True)
            os.link(root / existing, root / link)
        config = RootConfig(id="r", path=str(root), label="r")
        ctx = ValidationContext(mounts=data_disk_table(str(tmp_path)))
        request = ScanRequest(root=config, subpath=subpath)
        scan_id = run_scan(conn, request, ctx=ctx, cancel=threading.Event()).scan_id
        if search:
            run_duplicates(
                conn, scan_id, config, ctx=ctx, options=DupOptions(), cancel=threading.Event()
            )
        return similar_folders(conn, scan_id)

    return run


def only(report: Report | None) -> Comparison:
    assert report is not None
    assert len(report.pairs) == 1, [(p.a_path, p.b_path) for p in report.pairs]
    return report.pairs[0]


def paths(listing: object) -> list[object]:
    return [
        getattr(item, "path", None) or (item.path_a, item.path_b)  # type: ignore[attr-defined]
        for item in listing.items  # type: ignore[attr-defined]
    ]


PHOTOS = {f"2019/Noël/{n}.jpg": blob(n) for n in ("a", "b", "c")} | {"notes.txt": blob("n", 2000)}


def test_a_renamed_copy_is_one_pair_identical_both_ways(
    analyse: Callable[..., Report | None],
) -> None:
    tree = {f"Sauvegarde/{p}": c for p, c in PHOTOS.items()}
    tree |= {f"Sauvegarde (copie)/{p}": c for p, c in PHOTOS.items()}
    pair = only(analyse(tree))
    assert {pair.a_path, pair.b_path} == {"Sauvegarde", "Sauvegarde (copie)"}
    assert pair.score == 1.0 and pair.same.count == 4
    assert pair.contains("a") and pair.contains("b")
    assert pair.moved.count == pair.modified.count == pair.only_a.count == pair.only_b.count == 0


def test_a_subset_is_told_which_way(analyse: Callable[..., Report | None]) -> None:
    tree = {f"Vieux/{p}": c for p, c in PHOTOS.items()}
    tree |= {f"Nouveau/{p}": c for p, c in PHOTOS.items()} | {"Nouveau/2020/d.jpg": blob("d")}
    pair = only(analyse(tree))
    big = "a" if pair.a_path == "Nouveau" else "b"
    assert pair.contains(big) and not pair.contains("b" if big == "a" else "a")
    extra = pair.only_a if big == "a" else pair.only_b
    assert paths(extra) == ["2020/d.jpg"]
    small = "b" if big == "a" else "a"
    assert pair.found_in_other(small) == 1.0  # all of the old one is in the new one
    assert pair.found_in_other(big) == pytest.approx(14_000 / 18_000)  # bytes: 4 of 5 files
    assert pair.score == 1.0


def test_modified_moved_and_missing_files(analyse: Callable[..., Report | None]) -> None:
    common = {f"{n}.jpg": blob(n) for n in "abcdef"}
    tree = {f"A/{p}": c for p, c in common.items()} | {f"B/{p}": c for p, c in common.items()}
    tree |= {"A/edit.doc": blob("v1"), "B/edit.doc": blob("v2")}  # same size, other content
    tree |= {"A/grown.doc": blob("g", 3000), "B/grown.doc": blob("g", 5000)}
    tree |= {"A/old name.jpg": blob("m"), "B/sub/new name.jpg": blob("m")}
    tree |= {"A/lost.jpg": blob("lost")}
    pair = only(analyse(tree))
    assert pair.a_path == "A"
    assert pair.same.count == 6
    assert sorted(paths(pair.modified)) == ["edit.doc", "grown.doc"]
    assert paths(pair.moved) == [("old name.jpg", "sub/new name.jpg")]
    assert paths(pair.only_a) == ["lost.jpg"] and pair.only_b.count == 0
    assert not pair.contains("a") and not pair.contains("b")


def test_small_files_are_a_supposition(analyse: Callable[..., Report | None]) -> None:
    common = {f"{n}.jpg": blob(n) for n in "abc"}
    tree = {f"A/{p}": c for p, c in common.items()} | {f"B/{p}": c for p, c in common.items()}
    tree |= {"A/tiny.txt": b"hello", "B/tiny.txt": b"world"}  # below 1 KiB: never read
    tree |= {"A/short.txt": b"abc", "B/short.txt": b"abcdef"}  # another size: a fact
    pair = only(analyse(tree))
    assert paths(pair.unverified) == ["tiny.txt"]
    assert paths(pair.modified) == ["short.txt"]
    # Shares count known contents only: 3 of 4 files known identical.
    assert pair.found_in_other("a") == pytest.approx(3 / 4)
    assert not pair.contains("a")  # never claimed on a supposition


def test_hard_links_and_empty_files(analyse: Callable[..., Report | None]) -> None:
    common = {f"{n}.jpg": blob(n) for n in "abc"}
    tree = {f"A/{p}": c for p, c in common.items()} | {f"B/{p}": c for p, c in common.items()}
    tree |= {"A/linked.bin": blob("unique size", 7777), "A/empty": b"", "B/empty": b""}
    pair = only(analyse(tree, links={"B/linked.bin": "A/linked.bin"}))
    assert "linked.bin" in paths(pair.same) and "empty" in paths(pair.same)
    assert pair.score == 1.0


def test_a_pair_inside_a_pair_is_not_repeated(analyse: Callable[..., Report | None]) -> None:
    inner = {f"x/{n}.jpg": blob(n) for n in "abc"} | {f"y/{n}.jpg": blob(n + "y") for n in "abc"}
    tree = {f"A/{p}": c for p, c in inner.items()}
    # B's sub-folders swapped names: x/y pairs inside A/B, found on their own.
    tree |= {f"B/{p.replace('x/', 'z/')}": c for p, c in inner.items()}
    report = analyse(tree)
    assert report is not None
    assert [(p.a_path, p.b_path) for p in report.pairs] == [("A", "B")]


def test_unrelated_folders_make_no_pair(analyse: Callable[..., Report | None]) -> None:
    tree = {f"A/{n}.jpg": blob(n) for n in "abcdef"} | {
        f"B/{n}.jpg": blob(n + "B") for n in "abcdef"
    }
    tree |= {"A/shared.jpg": blob("s"), "B/shared.jpg": blob("s")}  # one file only
    report = analyse(tree)
    assert report is not None and report.pairs == []


def test_nothing_is_said_without_a_search_for_duplicates(
    analyse: Callable[..., Report | None],
) -> None:
    tree = {f"A/{p}": c for p, c in PHOTOS.items()} | {f"B/{p}": c for p, c in PHOTOS.items()}
    assert analyse(tree, search=False) is None


def test_hostile_names_stay_plain_data(analyse: Callable[..., Report | None]) -> None:
    hostile = "$(touch PWNED) <b>x</b>"
    tree = {f"{hostile}/{p}": c for p, c in PHOTOS.items()} | {
        f"B/{p}": c for p, c in PHOTOS.items()
    }
    pair = only(analyse(tree))
    assert hostile in (pair.a_path, pair.b_path) and pair.score == 1.0


def test_many_copies_are_one_family(analyse: Callable[..., Report | None]) -> None:
    tree: Tree = {}
    for copy in ("A", "B", "C", "D"):
        tree |= {f"{copy}/{p}": c for p, c in PHOTOS.items()}
    tree |= {"D/extra.jpg": blob("extra")}  # the biggest: the reference
    report = analyse(tree)
    assert report is not None and len(report.families) == 1
    family = report.families[0]
    assert family.members == 4 and family.reference == "D"
    assert sorted(c.b_path for c in family.comparisons) == ["A", "B", "C"]
    assert all(c.a_path == "D" and c.contains("a") for c in family.comparisons)


def test_the_small_files_search_turns_suppositions_into_facts(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> None:
    root = tmp_path / "root"
    tree = {f"A/{n}.jpg": blob(n) for n in "abc"} | {f"B/{n}.jpg": blob(n) for n in "abc"}
    tree |= {
        "A/tiny.txt": b"hello",
        "B/tiny.txt": b"world",
        "A/same.txt": b"ok",
        "B/same.txt": b"ok",
    }
    for path, content in tree.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    config = RootConfig(id="r", path=str(root), label="r")
    ctx = ValidationContext(mounts=data_disk_table(str(tmp_path)))
    scan_id = run_scan(conn, ScanRequest(root=config), ctx=ctx, cancel=threading.Event()).scan_id
    for options in (DupOptions(), DupOptions.for_micro()):
        run_duplicates(conn, scan_id, config, ctx=ctx, options=options, cancel=threading.Event())
    report = similar_folders(conn, scan_id)
    assert report is not None and report.small_files_read
    pair = only(report)
    assert pair.unverified.count == 0
    assert paths(pair.modified) == ["tiny.txt"] and "same.txt" in paths(pair.same)


def test_an_analysis_of_a_sub_folder(analyse: Callable[..., Report | None]) -> None:
    tree = {f"Disque/Sauvegarde/{p}": c for p, c in PHOTOS.items()}
    tree |= {f"Disque/Copie/{p}": c for p, c in PHOTOS.items()}
    tree |= {f"Ailleurs/{p}": c for p, c in PHOTOS.items()}  # outside the analysis
    pair = only(analyse(tree, subpath="Disque"))
    assert {pair.a_path, pair.b_path} == {"Disque/Sauvegarde", "Disque/Copie"}
    assert pair.score == 1.0


def test_a_backup_inside_a_sub_folder_pairs_with_the_original(
    analyse: Callable[..., Report | None],
) -> None:
    # "Photos" and "Sauvegarde/Photos": the analysed folder and "Sauvegarde"
    # would be a folder and its own sub-folder.
    tree = {f"Photos/{p}": c for p, c in PHOTOS.items()}
    tree |= {f"Sauvegarde/Photos/{p}": c for p, c in PHOTOS.items()}
    pair = only(analyse(tree))
    assert {pair.a_path, pair.b_path} == {"Photos", "Sauvegarde/Photos"}
    assert pair.score == 1.0
