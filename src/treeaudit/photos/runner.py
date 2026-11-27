"""Reading the dates the photos below a folder were taken (D-071).

Started by the user from Ranger, like the search for duplicates: the analysis
itself never opens a file. Each photo is opened through TreeReader (no symlink
is ever followed, the file must still be the one of the index before and after
it is read) and at most exif.MAX_READ bytes of it are read. Hard links are read
once. The files are read in the order of their position on the disk, asked
first (rotating disks pay 10-15 ms per seek). What was read is stored as it
goes, so a cancelled or interrupted reading keeps its work. The run ends
complete, incomplete (some photos could not be read), cancelled or failed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ScanLock, prepare_data_dir
from treeaudit.core.errors import TreeAuditError
from treeaudit.db import photos as photos_db
from treeaudit.db import scans
from treeaudit.db.connection import connect
from treeaudit.duplicates.placement import physical_offset, read_order_key
from treeaudit.duplicates.reader import Expected, FileChangedError, TreeReader, check_unchanged
from treeaudit.duplicates.runner import expected_root_of
from treeaudit.photos import exif
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext, validate_configured_root
from treeaudit.scanner.models import ScanStatus

logger = logging.getLogger(__name__)

SAVE_EVERY = 200
"""Results stored per transaction: a cancelled reading loses at most these."""


class PhotoDatesError(TreeAuditError):
    """The reading cannot start (unknown scan or folder, another root)."""


@dataclass(slots=True)
class DatesProgress:
    """Written by the reading thread only; other threads just read it."""

    stage: str = ""
    total: int = 0
    """Photos below the folder."""
    reused: int = 0
    """Already read, by this analysis or an earlier one."""
    stage_done: int = 0
    stage_total: int = 0
    dated: int = 0
    """Photos of this run that carry a date."""
    errors: int = 0
    bytes_read: int = 0

    def start_stage(self, name: str, files: int) -> None:
        """A new stage begins: its own counters start from zero."""
        self.stage, self.stage_done, self.stage_total = name, 0, files


@dataclass(frozen=True, slots=True)
class DatesOutcome:
    """How a reading ended. ``result_url`` goes back to Ranger on the folder."""

    status: ScanStatus
    total: int
    read: int
    dated: int
    errors: int
    reused: int
    result_url: str
    failure: str | None = None


def _expected(photo: photos_db.Photo) -> Expected:
    return Expected(photo.dev, photo.ino, photo.size, photo.mtime_ns)


def _inside(rel_path: bytes, folders: frozenset[bytes]) -> bool:
    return any(rel_path == f or rel_path.startswith(f + b"/") for f in folders)


class _Reading:
    """One run over the photos to read. Results wait in ``batch`` until
    stored; ``failures`` keeps why a file could not be read, by entry id."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        scan_id: int,
        reader: TreeReader,
        cancel: threading.Event,
        progress: DatesProgress,
    ) -> None:
        self.conn = conn
        self.scan_id = scan_id
        self.reader = reader
        self.cancel = cancel
        self.progress = progress
        self.batch: list[photos_db.Result] = []
        self.failures: dict[int, str] = {}
        self.read = 0

    def _store(
        self, links: list[photos_db.Photo], taken: datetime | None, failure: str | None
    ) -> None:
        self.batch.extend(photos_db.Result(p.entry_id, taken, failure) for p in links)
        if len(self.batch) >= SAVE_EVERY:
            self.flush()

    def flush(self) -> None:
        """Store the results waiting in the batch."""
        if self.batch:
            photos_db.save(self.conn, self.scan_id, self.batch)
            self.batch = []

    def _fail(self, photo: photos_db.Photo, failure: str) -> None:
        self.progress.errors += 1
        self.failures[photo.entry_id] = failure

    def _open(self, photo: photos_db.Photo) -> int | None:
        """The open file, or None once its failure is recorded."""
        try:
            return self.reader.open_file(photo.rel_path, _expected(photo))
        except FileChangedError:
            self._fail(photo, "changed")
        except OSError:
            self._fail(photo, "unreadable")
        return None

    def _locate(self, photos: list[photos_db.Photo]) -> dict[int, tuple[int, int, int]]:
        """The position of each file on the disk, in path order (directories
        are reused). A file that cannot be opened is recorded as failed."""
        self.progress.start_stage("repérage sur le disque", len(photos))
        order: dict[int, tuple[int, int, int]] = {}
        for photo in photos:
            if self.cancel.is_set():
                break
            fd = self._open(photo)
            self.progress.stage_done += 1
            if fd is None:
                continue
            try:
                order[photo.entry_id] = read_order_key(physical_offset(fd), photo.dev, photo.ino)
            finally:
                os.close(fd)
        return order

    def _read_one(self, photo: photos_db.Photo) -> datetime | None:
        """The date of one photo; None without a date or once its failure is
        recorded."""
        fd = self._open(photo)
        if fd is None:
            return None
        expected = _expected(photo)

        def read(offset: int, length: int) -> bytes:
            data = os.pread(fd, length, offset)
            self.progress.bytes_read += len(data)
            return data

        try:
            taken = exif.taken_at(read, photo.size)
            check_unchanged(fd, expected)
        except FileChangedError:
            self._fail(photo, "changed")
            return None
        except OSError:
            self._fail(photo, "unreadable")
            return None
        finally:
            os.close(fd)
        return taken

    def run(self, photos: list[photos_db.Photo]) -> bool:
        """Read every photo once per inode (hard links share the result);
        False when cancelled."""
        links: dict[tuple[int, int], list[photos_db.Photo]] = defaultdict(list)
        for photo in photos:
            links[(photo.dev, photo.ino)].append(photo)
        first = [group[0] for group in links.values()]
        order = self._locate(first)
        for photo in first:
            if photo.entry_id in self.failures:
                self._store(links[(photo.dev, photo.ino)], None, self.failures[photo.entry_id])
        if self.cancel.is_set():
            return False
        located = sorted((p for p in first if p.entry_id in order), key=lambda p: order[p.entry_id])
        self.progress.start_stage("lecture des dates", len(located))
        for photo in located:
            if self.cancel.is_set():
                return False
            taken = self._read_one(photo)
            self.read += 1
            self.progress.stage_done += 1
            group = links[(photo.dev, photo.ino)]
            if taken is not None:
                self.progress.dated += len(group)
            self._store(group, taken, self.failures.get(photo.entry_id))
        return True


def run_photo_dates(
    conn: sqlite3.Connection,
    scan_id: int,
    start_id: int,
    root: RootConfig,
    *,
    ctx: ValidationContext,
    cancel: threading.Event,
    progress: DatesProgress | None = None,
) -> DatesOutcome:
    """Read the dates of the photos below folder ``start_id`` of a recorded
    scan of ``root``. The root is validated again and must still be the folder
    the scan saw. Raises PhotoDatesError if the scan or folder is unknown or
    the scan was made on another root; everything else ends in an outcome. The
    caller (execute_photo_dates) holds the lock."""
    scan = scans.get_scan(conn, scan_id)
    if scan is None:
        raise PhotoDatesError("analyse inconnue")
    if scan["root_id"] != root.id or scan["root_path"] != root.path:
        raise PhotoDatesError("cette analyse a été faite sur un autre dossier : relance-la")
    folder = conn.execute(
        "SELECT kind FROM entry WHERE scan_id = ? AND entry_id = ?", (scan_id, start_id)
    ).fetchone()
    if folder is None or folder["kind"] != "dir":
        raise PhotoDatesError("ce dossier n'existe pas dans cette analyse")
    validate_configured_root(root, ctx)
    expected_root = expected_root_of(conn, scan, root.path)

    progress = progress if progress is not None else DatesProgress()
    state = photos_db.pending(conn, scan_id, start_id, extensions=exif.EXTENSIONS)
    # Private folders are never scanned, so never in the index; checked again
    # anyway: a photo inside one is never opened.
    todo = [p for p in state.todo if not _inside(p.rel_path, root.private_paths)]
    progress.total = state.total
    progress.reused = state.total - len(state.todo)

    status = ScanStatus.COMPLETE
    failure: str | None = None
    reading: _Reading | None = None
    try:
        with TreeReader(root.path, expected_root) as reader:
            reading = _Reading(conn, scan_id, reader, cancel, progress)
            if not reading.run(todo):
                status = ScanStatus.CANCELLED
            reading.flush()
    except Exception as exc:
        logger.exception("reading the photo dates of scan %d failed", scan_id)
        status = ScanStatus.FAILED
        failure = f"{type(exc).__name__}: {exc}"
        if reading is not None:
            try:
                reading.flush()  # keep what was read before the failure
            except sqlite3.Error:
                logger.exception("could not store the photo dates read so far")
    if status is ScanStatus.COMPLETE and progress.errors:
        status = ScanStatus.INCOMPLETE
    return DatesOutcome(
        status=status,
        total=state.total,
        read=reading.read if reading is not None else 0,
        dated=progress.dated,
        errors=progress.errors,
        reused=progress.reused,
        result_url=f"/scans/{scan_id}/organize?dir={start_id}",
        failure=failure,
    )


def execute_photo_dates(
    config: AppConfig,
    scan_id: int,
    start_id: int,
    *,
    mounts: MountTable,
    home: str | None,
    cancel: threading.Event,
    progress: DatesProgress | None = None,
) -> DatesOutcome:
    """Take the lock (one heavy job at a time) and read the dates."""
    prepare_data_dir(config.data_dir)
    ctx = ValidationContext(
        mounts=mounts,
        home=home,
        data_dir=config.data_dir,
        check_filesystem=True,
        require_read_only_mount=config.container_mode,
    )
    with ScanLock(config.data_dir):
        conn = connect(config.data_dir)
        try:
            scan = scans.get_scan(conn, scan_id)
            if scan is None:
                raise PhotoDatesError("analyse inconnue")
            if scan["status"] in ("pending", "running"):
                raise PhotoDatesError("l'analyse n'est pas terminée")
            root = config.get_root(scan["root_id"])
            if root is None:
                raise PhotoDatesError(
                    "le dossier de cette analyse n'est plus déclaré dans la configuration"
                )
            return run_photo_dates(
                conn, scan_id, start_id, root, ctx=ctx, cancel=cancel, progress=progress
            )
        finally:
            conn.close()
