"""Creating a verified archive of a folder of a root (D-024, D-032).

Steps, each of which can stop the archive before anything is trusted:

1. revalidate the archive directory and the root, take the archive lock;
2. walk the folder once (metadata only) to know its size;
3. check the free space for the worst case (incompressible data);
4. write the archive under a temporary name, reading every file through
   descriptors opened without following symlinks (the walker and TreeReader
   used by the scan and the duplicate search);
5. read the archive back and compare every member with what was read;
6. publish it under its final name without replacing anything, or remove the
   temporary file on failure or cancellation.

The audited folder is only ever read.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tarfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from treeaudit.archive.destination import (
    ArchiveDirContext,
    ArchiveDirReason,
    ArchiveDirRejectedError,
    validate_archive_dir,
)
from treeaudit.archive.output import ArchiveOutput, Compression, OutputError, TarStream
from treeaudit.archive.source import CancelledError, HashingReader
from treeaudit.archive.verify import (
    KIND_DIR,
    KIND_FILE,
    KIND_SYMLINK,
    Manifest,
    fingerprint,
    printable,
    verify_archive,
)
from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ArchiveLock, prepare_data_dir
from treeaudit.core.errors import TreeAuditError
from treeaudit.duplicates.reader import Expected, FileChangedError, TreeReader, check_unchanged
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext, validate_root
from treeaudit.scanner.models import DirDone, EntryKind, EntryRecord, ErrorRecord, ExclusionRecord
from treeaudit.scanner.runner import mount_points_below
from treeaudit.scanner.walker import DEFAULT_MAX_DEPTH, WalkOptions, parse_subpath, walk

logger = logging.getLogger(__name__)

MAX_LISTED_PROBLEMS = 200
# Worst case: incompressible data, plus tar headers (PAX ones included) and a margin.
_HEADER_ALLOWANCE = 1536
_FIXED_MARGIN = 16 * 1024 * 1024
_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
# Used only for a warning, never to allow or refuse anything.
_WHOLE_DISK = re.compile(r"^(/dev/(?:nvme\d+n\d+|mmcblk\d+|[a-z]+))(?:p?\d+)?$")


class ArchiveStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Stage(StrEnum):
    PREPARING = "preparing"
    WRITING = "writing"
    VERIFYING = "verifying"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    root: RootConfig
    subpath: str = ""
    compression: Compression = Compression.GZ
    max_depth: int = DEFAULT_MAX_DEPTH
    expected_folder: tuple[int, int] | None = None
    """(st_dev, st_ino) the folder must have, when it was chosen in an analysis:
    the root may point to another folder since, with the same relative path."""


@dataclass(slots=True)
class ArchiveProgress:
    """Written by the archiving thread only; other threads just read it."""

    stage: Stage = Stage.PREPARING
    entries_total: int = 0
    bytes_total: int = 0
    entries_done: int = 0
    bytes_read: int = 0
    bytes_verified: int = 0
    problems: int = 0


@dataclass(frozen=True, slots=True)
class ArchiveProblem:
    path: str
    """Path relative to the root, printable (D-014)."""
    message: str


@dataclass(slots=True)
class ArchiveOutcome:
    status: ArchiveStatus
    archive_name: str | None = None
    archive_dir_shown: str | None = None
    entries: int = 0
    bytes_read: int = 0
    archive_size: int = 0
    verified: bool = False
    same_disk: bool = False
    problems: list[ArchiveProblem] = field(default_factory=list)
    problem_count: int = 0
    failure: str | None = None

    @property
    def archive_path_shown(self) -> str | None:
        if self.archive_name is None or self.archive_dir_shown is None:
            return None
        return self.archive_dir_shown.rstrip("/") + "/" + self.archive_name


class InsufficientSpaceError(TreeAuditError):
    """Not enough free space in the archive directory for the worst case."""

    def __init__(self, needed: int, available: int) -> None:
        self.needed = needed
        self.available = available
        super().__init__(f"not enough free space: {needed} bytes needed, {available} available")


def estimate_needed_space(file_bytes: int, entries: int) -> int:
    """Upper bound of the archive size (data that does not compress at all)."""
    return file_bytes + file_bytes // 100 + entries * _HEADER_ALLOWANCE + _FIXED_MARGIN


def archive_base_name(folder_name: bytes) -> str:
    """Portable file name stem derived from the archived folder's name."""
    text = _NAME_CHARS.sub("_", folder_name.decode("utf-8", errors="replace"))
    text = text.lstrip("._-")[:80].rstrip("._-")
    return text or "archive"


def same_disk(mounts: MountTable, first: str, second: str) -> bool:
    """Best effort: are both paths on the same filesystem or physical disk?"""
    a = mounts.mount_for(first)
    b = mounts.mount_for(second)
    if a is None or b is None:
        return False
    if a.major_minor == b.major_minor:
        return True
    disk_a = _WHOLE_DISK.match(a.source)
    disk_b = _WHOLE_DISK.match(b.source)
    return disk_a is not None and disk_b is not None and disk_a.group(1) == disk_b.group(1)


def execute_archive(
    config: AppConfig,
    request: ArchiveRequest,
    *,
    mounts: MountTable,
    home: str | None,
    cancel: threading.Event,
    progress: ArchiveProgress | None = None,
    now: datetime | None = None,
) -> ArchiveOutcome:
    """Archive ``request.subpath`` of ``request.root`` into the archive directory.

    Raises before writing anything if the archive directory or the root is not
    acceptable (ArchiveDirRejectedError, RootRejectedError, SubpathError), if
    another archive is running (ArchiveLockedError) or if the free space is
    insufficient (InsufficientSpaceError). Everything else ends in an outcome.
    """
    if config.archive_dir is None:
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_CONFIGURED, "")
    validate_archive_dir(
        config.archive_dir,
        ArchiveDirContext(
            mounts=mounts,
            home=home,
            data_dir=config.data_dir,
            root_paths=tuple(root.path for root in config.declared_roots),
            container_mode=config.container_mode,
        ),
    )
    root = request.root
    subpath = parse_subpath(request.subpath)
    validate_root(
        root.path,
        ValidationContext(
            mounts=mounts,
            home=home,
            data_dir=config.data_dir,
            require_read_only_mount=config.container_mode,
        ),
        allow_system_volume=root.allow_system_volume,
    )
    prepare_data_dir(config.data_dir)
    with ArchiveLock(config.data_dir):
        job = _ArchiveJob(
            config=config,
            archive_dir=config.archive_dir,
            request=request,
            subpath=subpath,
            mounts=mounts,
            cancel=cancel,
            progress=progress if progress is not None else ArchiveProgress(),
        )
        return job.run(now or datetime.now())


class _ArchiveJob:
    def __init__(
        self,
        *,
        config: AppConfig,
        archive_dir: str,
        request: ArchiveRequest,
        subpath: tuple[str, ...],
        mounts: MountTable,
        cancel: threading.Event,
        progress: ArchiveProgress,
    ) -> None:
        self.config = config
        self.archive_dir = archive_dir
        self.request = request
        self.root = request.root
        self.subpath = subpath
        self.mounts = mounts
        self.cancel = cancel
        self.progress = progress
        root_stat = os.lstat(self.root.path)
        self.expected_root = (root_stat.st_dev, root_stat.st_ino)
        self.options = WalkOptions(
            root_path=self.root.path,
            mount_points=mount_points_below(mounts, self.root.path),
            cross_mounts=self.root.cross_mounts,
            max_depth=request.max_depth,
            expected_root=self.expected_root,
        )
        self.outcome = ArchiveOutcome(
            status=ArchiveStatus.FAILED,
            archive_dir_shown=config.shown_archive_dir,
            same_disk=same_disk(mounts, self.root.path, archive_dir),
        )

    # --- helpers ------------------------------------------------------------

    def _problem(self, rel_path: bytes, message: str) -> None:
        self.outcome.problem_count += 1
        self.progress.problems = self.outcome.problem_count
        if len(self.outcome.problems) < MAX_LISTED_PROBLEMS:
            self.outcome.problems.append(ArchiveProblem(printable(rel_path) or ".", message))

    def _top_name(self) -> bytes:
        if self.subpath:
            return os.fsencode(self.subpath[-1])
        shown = os.path.basename(self.root.shown_path.rstrip("/"))
        return os.fsencode(shown) if shown else b"racine"

    def _on_read(self, count: int) -> None:
        self.progress.bytes_read += count

    def _on_verify(self, count: int) -> None:
        self.progress.bytes_verified += count

    # --- steps --------------------------------------------------------------

    def run(self, now: datetime) -> ArchiveOutcome:
        if not self._measure():
            self.progress.stage = Stage.DONE
            return self.outcome
        before = os.lstat(self.archive_dir)
        with ArchiveOutput(self.archive_dir) as output:
            if output.directory_identity() != (before.st_dev, before.st_ino):
                raise OutputError("the archive directory changed since its validation")
            needed = estimate_needed_space(self.progress.bytes_total, self.progress.entries_total)
            available = output.free_bytes()
            if needed > available:
                raise InsufficientSpaceError(needed, available)
            stem = f"{archive_base_name(self._top_name())}-{now:%Y%m%d-%H%M%S}"
            suffix = self.request.compression.suffix
            published = False
            try:
                manifest = self._write(
                    output, f".{stem}{suffix}.partial", output.directory_identity()
                )
                self.outcome.archive_size = output.sync()
                output.close_file()
                if not self._verify(output, manifest):
                    return self.outcome
                incomplete = self.outcome.problem_count > 0
                final = f"{stem}-INCOMPLET{suffix}" if incomplete else f"{stem}{suffix}"
                self.outcome.archive_name = output.publish(final)
                published = True
                self.outcome.status = (
                    ArchiveStatus.INCOMPLETE if incomplete else ArchiveStatus.COMPLETE
                )
            except CancelledError:
                self.outcome.status = ArchiveStatus.CANCELLED
            except Exception as exc:
                logger.exception("archive failed")
                self.outcome.status = ArchiveStatus.FAILED
                self.outcome.failure = f"{type(exc).__name__}: {exc}"
            finally:
                if not published:
                    output.discard()
                self.progress.stage = Stage.DONE
        return self.outcome

    def _measure(self) -> bool:
        """First walk (metadata only): number of entries and bytes to archive.

        Also refuses, before anything is written, a folder that contains the
        archive directory (possible when it is on a detected external disk).
        """
        archive_stat = os.stat(self.archive_dir)
        archive_id = (archive_stat.st_dev, archive_stat.st_ino)
        events = walk(self.options, self.subpath)
        try:
            for event in events:
                if self.cancel.is_set():
                    self.outcome.status = ArchiveStatus.CANCELLED
                    return False
                if isinstance(event, EntryRecord):
                    expected = self.request.expected_folder
                    if event.depth == 0 and expected not in (None, (event.dev, event.ino)):
                        self.outcome.failure = (
                            "ce dossier n'est plus celui de l'analyse (le dossier analysé a "
                            "changé ou a été remplacé) : relance une analyse"
                        )
                        return False
                    if event.kind is EntryKind.DIR and (event.dev, event.ino) == archive_id:
                        self.outcome.failure = (
                            "ce dossier contient le dossier d'archives : choisir un dossier "
                            "qui ne le contient pas"
                        )
                        return False
                    self.progress.entries_total += 1
                    if event.kind is EntryKind.FILE:
                        self.progress.bytes_total += event.size
                elif isinstance(event, ErrorRecord) and self.progress.entries_total == 0:
                    # The folder itself cannot be opened: nothing to archive.
                    self.outcome.failure = f"{printable(event.rel_path) or '.'} : {event.message}"
                    return False
        finally:
            events.close()
        return True

    def _write(
        self, output: ArchiveOutput, partial: str, archive_dir_id: tuple[int, int]
    ) -> Manifest:
        self.progress.stage = Stage.WRITING
        manifest = Manifest()
        top = self._top_name()
        prefix = os.fsencode("/".join(self.subpath)) + b"/" if self.subpath else b""
        tar = output.open_tar(partial, self.request.compression)
        events = walk(self.options, self.subpath)
        try:
            with tar, TreeReader(self.root.path, self.expected_root) as reader:
                # The walker lists a whole directory before descending into its
                # subdirectories. tar expects each directory to be followed by its
                # own content (GNU tar restores a directory's date as soon as it
                # meets a member outside it), so a subdirectory's header is held
                # back until its content starts, or until the walker is done with it.
                pending: dict[int, EntryRecord] = {}
                pending_by_path: dict[bytes, int] = {}

                def add(record: EntryRecord) -> None:
                    rel = record.rel_path[len(prefix) :]
                    name = top if record.depth == 0 else top + b"/" + rel
                    self._add(tar, reader, manifest, record, name, archive_dir_id)

                def release(entry_id: int | None) -> None:
                    record = pending.pop(entry_id, None) if entry_id is not None else None
                    if record is not None:
                        del pending_by_path[record.rel_path]
                        add(record)

                for event in events:
                    if self.cancel.is_set():
                        raise CancelledError
                    if isinstance(event, DirDone):
                        release(event.entry_id)  # empty directory
                    elif isinstance(event, ErrorRecord | ExclusionRecord):
                        release(pending_by_path.get(event.rel_path))  # kept, but empty
                        if isinstance(event, ErrorRecord):
                            self._problem(event.rel_path, f"illisible : {event.message}")
                        else:
                            self._problem(event.rel_path, "autre disque monté ici : non archivé")
                    else:
                        release(event.parent_id)
                        if event.kind is EntryKind.DIR and event.depth > 0:
                            pending[event.entry_id] = event
                            pending_by_path[event.rel_path] = event.entry_id
                        else:
                            add(event)
                for entry_id in list(pending):  # defensive: the walker visits them all
                    release(entry_id)
        finally:
            events.close()
        return manifest

    def _add(
        self,
        tar: TarStream,
        reader: TreeReader,
        manifest: Manifest,
        record: EntryRecord,
        name: bytes,
        archive_dir_id: tuple[int, int],
    ) -> None:
        self.progress.entries_done += 1
        if record.kind is EntryKind.DIR:
            if (record.dev, record.ino) == archive_dir_id:
                raise OutputError("the archive directory is inside the archived folder")
            tar.add(_tar_info(name, record, tarfile.DIRTYPE))
            manifest.add(fingerprint(KIND_DIR, name, record.mode, 0, b"", b""))
        elif record.kind is EntryKind.SYMLINK:
            if record.link_target is None:
                return  # unreadable link: already reported by the walker
            info = _tar_info(name, record, tarfile.SYMTYPE)
            info.linkname = os.fsdecode(record.link_target)
            tar.add(info)
            manifest.add(fingerprint(KIND_SYMLINK, name, record.mode, 0, record.link_target, b""))
        elif record.kind is EntryKind.OTHER:
            self._problem(record.rel_path, "fichier spécial (fifo, socket, périphérique) ignoré")
        else:
            self._add_file(tar, reader, manifest, record, name)

    def _add_file(
        self,
        tar: TarStream,
        reader: TreeReader,
        manifest: Manifest,
        record: EntryRecord,
        name: bytes,
    ) -> None:
        expected = Expected(record.dev, record.ino, record.size, record.mtime_ns)
        try:
            fd = reader.open_file(record.rel_path, expected)
        except FileChangedError:
            self._problem(record.rel_path, "modifié ou remplacé depuis le parcours : non archivé")
            return
        except OSError as exc:
            self._problem(record.rel_path, f"illisible : {exc.strerror or exc}")
            return
        try:
            source = HashingReader(fd, record.size, cancel=self.cancel, on_read=self._on_read)
            info = _tar_info(name, record, tarfile.REGTYPE)
            info.size = record.size
            tar.add(info, source)
            source.done()
            if source.failure is not None:
                self._problem(record.rel_path, f"{source.failure} : copie incomplète")
            else:
                try:
                    check_unchanged(fd, expected)
                except FileChangedError:
                    self._problem(record.rel_path, "modifié pendant la lecture : copie incertaine")
            manifest.add(
                fingerprint(KIND_FILE, name, record.mode, record.size, b"", source.digest())
            )
        finally:
            os.close(fd)

    def _verify(self, output: ArchiveOutput, manifest: Manifest) -> bool:
        self.progress.stage = Stage.VERIFYING
        fd = output.open_partial_for_reading()
        try:
            result = verify_archive(
                fd,
                self.request.compression,
                manifest,
                cancel=self.cancel,
                on_read=self._on_verify,
            )
        finally:
            os.close(fd)
        self.outcome.entries = len(manifest)
        self.outcome.bytes_read = self.progress.bytes_read
        if not result.ok:
            self.outcome.status = ArchiveStatus.FAILED
            self.outcome.failure = f"vérification échouée : {result.problem}"
            return False
        self.outcome.verified = True
        return True


def _tar_info(name: bytes, record: EntryRecord, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(os.fsdecode(name))
    info.type = kind
    info.mode = stat.S_IMODE(record.mode)
    info.uid = record.uid
    info.gid = record.gid
    # Whole seconds, like GNU tar's default format: a sub-second time would need
    # an extra PAX header for every member, doubling the work to write and verify.
    info.mtime = record.mtime_ns // 1_000_000_000
    return info
