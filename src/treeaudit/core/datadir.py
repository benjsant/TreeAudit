"""Preparation of the TreeAudit data directory (index, reports).

This is one of the few modules allowed to write to disk, and only inside the
data directory, which never overlaps an audited root (docs/DECISIONS.md D-009).
"""

from __future__ import annotations

import fcntl
import logging
import os
import stat
from pathlib import Path

from treeaudit.core.errors import ConfigError

logger = logging.getLogger(__name__)

DATA_DIR_MODE = 0o700


def prepare_data_dir(data_dir: str) -> Path:
    """Create the data directory (mode 0700) if needed and check it is usable."""
    path = Path(data_dir)
    if not path.is_absolute():
        raise ConfigError(f"data_dir must be an absolute path: {data_dir!r}")
    try:
        path.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
        info = os.lstat(path)
    except OSError as exc:
        raise ConfigError(f"cannot create or access data_dir {data_dir!r}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"data_dir must be a real directory, not a link or file: {data_dir!r}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise ConfigError(f"data_dir is not writable: {data_dir!r}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        logger.warning(
            "data_dir %s is accessible to other users (mode %o); "
            "it will contain the full paths of audited files, consider chmod 700",
            data_dir,
            stat.S_IMODE(info.st_mode),
        )
    return path


LOCK_FILENAME = "scan.lock"
ARCHIVE_LOCK_FILENAME = "archive.lock"


class ScanLockedError(ConfigError):
    """Another scan is already running with this data directory."""


class ArchiveLockedError(ConfigError):
    """Another archive is already being written with this data directory."""


class ScanLock:
    """Exclusive, non-blocking lock: one scan at a time per data directory.

    Based on flock(2): the kernel releases it if the process dies, so a crash
    never leaves a stale lock behind.
    """

    filename = LOCK_FILENAME

    def __init__(self, data_dir: str) -> None:
        self.path = Path(data_dir) / self.filename
        self._fd: int | None = None

    def _locked_error(self) -> ConfigError:
        return ScanLockedError("another scan is already running")

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise self._locked_error() from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor releases the flock
            self._fd = None

    def __enter__(self) -> ScanLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class ArchiveLock(ScanLock):
    """One archive at a time: two would compete for the disk and the free space."""

    filename = ARCHIVE_LOCK_FILENAME

    def _locked_error(self) -> ConfigError:
        return ArchiveLockedError("another archive is already being written")


def own_dirs(*paths: str | None) -> frozenset[tuple[int, int]]:
    """(st_dev, st_ino) of TreeAudit's own folders (data, archives), as they exist now.

    A detected disk or the personal folder may contain them (D-037): they are
    then recognised by identity, never by name, and left out of every analysis.
    """
    identities: set[tuple[int, int]] = set()
    for path in paths:
        if path is None:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue  # not created yet: nothing of ours to skip
        identities.add((st.st_dev, st.st_ino))
    return frozenset(identities)
