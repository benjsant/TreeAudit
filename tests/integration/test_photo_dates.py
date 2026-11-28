"""Reading the dates photos were taken, on real files of a scanned tree (D-071)."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import struct
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db import photos as photos_db
from treeaudit.db.connection import connect_memory
from treeaudit.photos import exif, runner
from treeaudit.photos.runner import DatesOutcome, PhotoDatesError, run_photo_dates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.models import ScanStatus
from treeaudit.scanner.runner import ScanRequest, run_scan


def photo(taken: str | None, filler: int = 64) -> bytes:
    """A JPEG whose EXIF holds this DateTimeOriginal (none: an EXIF without a
    date), then a little image data."""
    if taken is None:
        tiff = b"II*\0" + struct.pack("<IHI", 8, 0, 0)
    else:
        value = taken.encode() + b"\0"
        tiff = (
            b"II*\0"
            + struct.pack("<I", 8)
            + struct.pack("<HHHIII", 1, 0x8769, 4, 1, 26, 0)  # IFD0 -> Exif IFD at 26
            + struct.pack("<HHHIII", 1, 0x9003, 2, len(value), 44, 0)  # date at 44
            + value
        )
    app1 = b"Exif\0\0" + tiff
    segment = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    return b"\xff\xd8" + segment + b"\xff\xda\0\x08" + b"\x55" * filler + b"\xff\xd9"


def video(recorded: datetime, *, filler: int = 64) -> bytes:
    """An MP4 whose movie header, after the image data, holds this creation
    time (UTC; the local time of the test machine decides the hour shown)."""
    since_1904 = int(recorded.timestamp()) + exif._MP4_EPOCH
    mvhd = struct.pack(">I4sI", 108, b"mvhd", 0) + struct.pack(
        ">IIII", since_1904, since_1904, 600, 0
    )
    mvhd += b"\0" * (108 - len(mvhd))
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 20, b"ftyp") + b"isom\0\0\0\0isom"
    mdat = struct.pack(">I4s", 8 + filler, b"mdat") + b"\x55" * filler
    return ftyp + mdat + moov


@dataclasses.dataclass
class Tree:
    """A scanned tree: run the reading on it, look at what was stored."""

    conn: sqlite3.Connection
    root: Path
    config: RootConfig
    ctx: ValidationContext
    scan_id: int = 0

    def scan(self) -> int:
        self.scan_id = run_scan(
            self.conn, ScanRequest(root=self.config), ctx=self.ctx, cancel=threading.Event()
        ).scan_id
        return self.scan_id

    def id_of(self, rel_path: str) -> int:
        if rel_path == "":
            row = self.conn.execute(
                "SELECT entry_id FROM entry WHERE scan_id = ? AND depth = 0", (self.scan_id,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT entry_id FROM entry WHERE scan_id = ? AND rel_path = ?",
                (self.scan_id, rel_path.encode()),
            ).fetchone()
        return int(row[0])

    def read(
        self,
        folder: str = "",
        *,
        cancel: threading.Event | None = None,
        config: RootConfig | None = None,
    ) -> DatesOutcome:
        return run_photo_dates(
            self.conn,
            self.scan_id,
            self.id_of(folder),
            config or self.config,
            ctx=self.ctx,
            cancel=cancel or threading.Event(),
        )

    def pending(self, folder: str = "") -> photos_db.Pending:
        return photos_db.pending(
            self.conn, self.scan_id, self.id_of(folder), extensions=exif.EXTENSIONS
        )

    def dates(self) -> dict[str, datetime]:
        names = {
            entry_id: path
            for entry_id, path in self.conn.execute(
                "SELECT entry_id, rel_path_display FROM entry WHERE scan_id = ?", (self.scan_id,)
            )
        }
        found = photos_db.taken_dates(self.conn, self.scan_id, names)
        return {names[entry_id]: taken for entry_id, taken in found.items()}

    def failures(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT e.rel_path_display, d.failure FROM photo_date d JOIN entry e "
            "ON e.scan_id = d.scan_id AND e.entry_id = d.entry_id "
            "WHERE d.scan_id = ? AND d.failure IS NOT NULL",
            (self.scan_id,),
        )
        return dict(rows.fetchall())


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def tree(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> Tree:
    root = tmp_path / "root"
    files = {
        "a.jpg": photo("2019:06:01 12:00:00"),
        "sans-date.jpg": photo(None),
        "capture.png": b"\x89PNG\r\n\x1a\n" + b"\0" * 40,
        "notes.txt": photo("2020:01:01 00:00:00"),  # not a photo extension: never read
        "Voyage/b.JPG": photo("2021:08:15 09:30:00"),
        "Voyage/c.heic": b"\0\0\0\x18ftypheic" + b"\0" * 40,
        "Voyage/Jour 2/d.jpg": photo("2021:08:16 18:45:10"),
    }
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    config = RootConfig(id="r", path=str(root), label="r")
    tree = Tree(conn, root, config, ValidationContext(mounts=data_disk_table(str(tmp_path))))
    tree.scan()
    return tree


def test_dates_are_read_and_stored(tree: Tree) -> None:
    outcome = tree.read()
    assert outcome.status is ScanStatus.COMPLETE
    assert (outcome.total, outcome.read, outcome.dated, outcome.errors) == (6, 6, 3, 0)
    assert outcome.result_url == f"/scans/{tree.scan_id}/organize?dir={tree.id_of('')}"
    assert tree.dates() == {
        "a.jpg": datetime(2019, 6, 1, 12, 0, 0),
        "Voyage/b.JPG": datetime(2021, 8, 15, 9, 30, 0),
        "Voyage/Jour 2/d.jpg": datetime(2021, 8, 16, 18, 45, 10),
    }
    state = tree.pending()
    assert (state.total, state.dated, state.undated, state.failed, state.todo) == (6, 3, 3, 0, ())


def test_only_the_photos_below_the_folder(tree: Tree) -> None:
    outcome = tree.read("Voyage")
    assert (outcome.total, outcome.read, outcome.dated) == (3, 3, 2)
    assert set(tree.dates()) == {"Voyage/b.JPG", "Voyage/Jour 2/d.jpg"}
    # The rest of the analysis is still to read; what was read is not again.
    state = tree.pending()
    assert {p.rel_path for p in state.todo} == {b"a.jpg", b"sans-date.jpg", b"capture.png"}
    assert tree.read().read == 3


def test_reading_stays_bounded(tree: Tree) -> None:
    (tree.root / "grand.jpg").write_bytes(photo("2022:02:02 02:02:02", filler=3 * exif.MAX_READ))
    tree.scan()
    progress = runner.DatesProgress()
    run_photo_dates(
        tree.conn,
        tree.scan_id,
        tree.id_of(""),
        tree.config,
        ctx=tree.ctx,
        cancel=threading.Event(),
        progress=progress,
    )
    assert tree.dates()["grand.jpg"] == datetime(2022, 2, 2, 2, 2, 2)
    assert progress.bytes_read < 7 * 1024  # the headers only, never the image data


def test_an_unchanged_file_is_not_read_again_by_the_next_analysis(tree: Tree) -> None:
    tree.read()
    first = tree.scan_id
    (tree.root / "a.jpg").write_bytes(photo("2018:03:03 03:03:03"))  # changed: read again
    tree.scan()
    assert tree.scan_id != first
    state = tree.pending()
    assert [p.rel_path for p in state.todo] == [b"a.jpg"]
    assert (state.dated, state.undated) == (2, 3)
    outcome = tree.read()
    assert (outcome.read, outcome.reused) == (1, 5)
    assert tree.dates()["a.jpg"] == datetime(2018, 3, 3, 3, 3, 3)


def test_another_reader_version_reads_everything_again(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.read()
    monkeypatch.setattr(photos_db, "READER", photos_db.READER + 1)
    assert len(tree.pending().todo) == 6
    assert tree.dates() == {}  # dates of another version are not used
    assert tree.read().read == 6


def test_hard_links_are_read_once(tree: Tree) -> None:
    os.link(tree.root / "a.jpg", tree.root / "Voyage" / "a-lien.jpg")
    tree.scan()
    outcome = tree.read()
    assert (outcome.total, outcome.read, outcome.dated) == (7, 6, 4)
    assert tree.dates()["Voyage/a-lien.jpg"] == tree.dates()["a.jpg"]


def test_a_file_changed_since_the_analysis_is_reported_and_asked_again(tree: Tree) -> None:
    path = tree.root / "a.jpg"
    stat = path.stat()
    path.write_bytes(photo("1999:09:09 09:09:09"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    outcome = tree.read()
    assert outcome.status is ScanStatus.INCOMPLETE
    assert outcome.errors == 1
    assert tree.failures() == {"a.jpg": "changed"}
    assert "a.jpg" not in tree.dates()
    state = tree.pending()
    assert state.failed == 1
    assert [p.rel_path for p in state.todo] == [b"a.jpg"]


def test_a_file_modified_while_it_is_read_gives_no_date(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = exif.taken_at
    path = tree.root / "a.jpg"

    def taken_at(read: exif.Reader, size: int) -> datetime | None:
        found = original(read, size)
        if found == datetime(2019, 6, 1, 12, 0, 0):  # a.jpg, rewritten under the reader
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        return found

    monkeypatch.setattr(exif, "taken_at", taken_at)
    outcome = tree.read()
    assert outcome.status is ScanStatus.INCOMPLETE
    assert tree.failures() == {"a.jpg": "changed"}
    assert "a.jpg" not in tree.dates()
    assert outcome.dated == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_an_unreadable_file_is_reported(tree: Tree) -> None:
    path = tree.root / "Voyage" / "b.JPG"
    path.chmod(0)
    try:
        outcome = tree.read()
    finally:
        path.chmod(0o644)
    assert outcome.status is ScanStatus.INCOMPLETE
    assert tree.failures() == {"Voyage/b.JPG": "unreadable"}
    # Readable again: the next reading takes it.
    assert tree.read().status is ScanStatus.COMPLETE
    assert tree.dates()["Voyage/b.JPG"] == datetime(2021, 8, 15, 9, 30, 0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_a_failure_is_not_carried_to_the_next_analysis(tree: Tree) -> None:
    """A photo unreadable in one analysis is not taken as read, without a
    date, by the next one: it is asked for again."""
    path = tree.root / "Voyage" / "b.JPG"
    path.chmod(0)  # the identity (inode, size, date) does not change
    try:
        tree.read()
    finally:
        path.chmod(0o644)
    tree.scan()
    state = tree.pending()
    assert [p.rel_path for p in state.todo] == [b"Voyage/b.JPG"]
    assert (state.dated, state.undated, state.failed) == (2, 3, 0)


def test_a_symlink_swapped_in_is_never_followed(tree: Tree, tmp_path: Path) -> None:
    elsewhere = tmp_path / "ailleurs"
    (elsewhere / "Jour 2").mkdir(parents=True)
    (elsewhere / "b.JPG").write_bytes(photo("2001:01:01 01:01:01"))
    (elsewhere / "Jour 2" / "d.jpg").write_bytes(photo("2001:01:01 01:01:01"))
    (tree.root / "Voyage").rename(tree.root / "Voyage-déplacé")
    (tree.root / "Voyage").symlink_to(elsewhere)
    outcome = tree.read()
    assert outcome.status is ScanStatus.INCOMPLETE
    assert tree.failures() == {
        "Voyage/b.JPG": "unreadable",
        "Voyage/c.heic": "unreadable",
        "Voyage/Jour 2/d.jpg": "unreadable",
    }
    assert set(tree.dates()) == {"a.jpg"}


def test_a_replaced_root_fails_without_reading(tree: Tree) -> None:
    tree.root.rename(tree.root.with_name("ancienne"))
    tree.root.mkdir()
    outcome = tree.read()
    assert outcome.status is ScanStatus.FAILED
    assert outcome.failure is not None and "RootChangedError" in outcome.failure
    assert outcome.read == 0
    assert tree.dates() == {}


def test_private_folders_are_never_opened(tree: Tree) -> None:
    private = dataclasses.replace(tree.config, private_paths=frozenset({b"Voyage/Jour 2"}))
    outcome = tree.read(config=private)
    assert outcome.read == 5
    assert "Voyage/Jour 2/d.jpg" not in tree.dates()


def test_cancelled_before_reading(tree: Tree) -> None:
    cancel = threading.Event()
    cancel.set()
    outcome = tree.read(cancel=cancel)
    assert outcome.status is ScanStatus.CANCELLED
    assert outcome.read == 0
    assert len(tree.pending().todo) == 6


def test_a_cancelled_reading_keeps_what_it_read(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "SAVE_EVERY", 2)
    cancel = threading.Event()
    original = runner._Reading._read_one
    calls = []

    def read_one(self: runner._Reading, item: photos_db.Photo) -> datetime | None:
        calls.append(item)
        if len(calls) == 4:
            cancel.set()
        return original(self, item)

    monkeypatch.setattr(runner._Reading, "_read_one", read_one)
    outcome = tree.read(cancel=cancel)
    assert outcome.status is ScanStatus.CANCELLED
    assert outcome.read == 4
    state = tree.pending()
    assert len(state.todo) == 2
    assert state.dated + state.undated == 4


def test_files_are_read_in_disk_order(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    # Positions on the disk in the reverse order of the paths.
    paths = sorted(p for p in tree.root.rglob("*") if p.is_file() and p.suffix != ".txt")
    position = {p.stat().st_ino: 1000 - i for i, p in enumerate(paths)}
    monkeypatch.setattr(runner, "physical_offset", lambda fd: position[os.fstat(fd).st_ino])
    order: list[bytes] = []
    original = runner._Reading._read_one

    def read_one(self: runner._Reading, item: photos_db.Photo) -> datetime | None:
        order.append(item.rel_path)
        return original(self, item)

    monkeypatch.setattr(runner._Reading, "_read_one", read_one)
    tree.read()
    expected = [os.fsencode(p.relative_to(tree.root)) for p in reversed(paths)]
    assert order == expected


def test_refused_requests(tree: Tree, tmp_path: Path) -> None:
    with pytest.raises(PhotoDatesError):
        tree.read("a.jpg")  # a file, not a folder
    other = RootConfig(id="autre", path=str(tmp_path), label="autre")
    with pytest.raises(PhotoDatesError):
        tree.read(config=other)
    with pytest.raises(PhotoDatesError):
        run_photo_dates(
            tree.conn,
            tree.scan_id + 99,
            1,
            tree.config,
            ctx=tree.ctx,
            cancel=threading.Event(),
        )


def test_stored_rows_are_checked(tree: Tree) -> None:
    entry_id = tree.id_of("a.jpg")
    with pytest.raises(ValueError, match="failure"):
        photos_db.save(tree.conn, tree.scan_id, [photos_db.Result(entry_id, None, "perdu")])
    with pytest.raises(ValueError, match="failure"):
        photos_db.save(
            tree.conn,
            tree.scan_id,
            [photos_db.Result(entry_id, datetime(2020, 1, 1), "changed")],
        )
    with pytest.raises(sqlite3.IntegrityError):  # the schema refuses it too
        tree.conn.execute(
            "INSERT INTO photo_date (scan_id, entry_id, reader, taken, failure) "
            "VALUES (?, ?, 1, '2020-01-01 00:00:00', 'changed')",
            (tree.scan_id, entry_id),
        )


def test_forgetting_the_analysis_forgets_its_dates(tree: Tree) -> None:
    from treeaudit.db import scans

    tree.read()
    assert scans.forget_scan(tree.conn, tree.scan_id)
    assert tree.conn.execute("SELECT COUNT(*) FROM photo_date").fetchone()[0] == 0


def test_a_video_is_dated_by_its_recording_time(tree: Tree) -> None:
    recorded = datetime(2022, 4, 3, 12, 0, 0, tzinfo=UTC)
    (tree.root / "Voyage" / "clip.mp4").write_bytes(video(recorded, filler=3 * exif.MAX_READ))
    (tree.root / "Voyage" / "vieux.mov").write_bytes(video(datetime(1904, 1, 1, tzinfo=UTC)))
    tree.scan()
    outcome = tree.read()
    assert (outcome.total, outcome.dated) == (8, 4)
    assert tree.dates()["Voyage/clip.mp4"] == recorded.astimezone().replace(tzinfo=None)
    assert "Voyage/vieux.mov" not in tree.dates()  # creation time unset
