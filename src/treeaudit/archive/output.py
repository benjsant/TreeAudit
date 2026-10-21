"""The only code allowed to write outside the data directory (D-024, D-032).

Every operation goes through a descriptor of the archive directory, opened
without following symlinks right after that directory was validated:

- the archive is created under a temporary name with O_CREAT | O_EXCL |
  O_NOFOLLOW: never an existing file, never through a symlink;
- it is published under its final name without ever replacing a file;
- the only removal TreeAudit performs is of that same temporary file, after
  checking it is still the inode this object created.

Nothing here takes a path from the audited data: names are built by the caller
from a sanitized folder name and a timestamp, and checked again here.
"""

from __future__ import annotations

import errno as errno_codes
import gzip
import io
import logging
import lzma
import os
import stat
import tarfile
from enum import StrEnum
from types import TracebackType
from typing import BinaryIO, Protocol

from treeaudit.core.errors import TreeAuditError

logger = logging.getLogger(__name__)

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
ARCHIVE_MODE = 0o600  # archives hold the user's data: private by default
WRITE_BUFFER = 1024 * 1024
MAX_NAME_ATTEMPTS = 100

# Hard links are not supported everywhere (FAT, exFAT on USB keys...).
_NO_LINK_ERRNOS = frozenset(
    {errno_codes.EPERM, errno_codes.EOPNOTSUPP, errno_codes.ENOTSUP, errno_codes.EMLINK}
)


class Compression(StrEnum):
    GZ = "gz"
    XZ = "xz"

    @property
    def suffix(self) -> str:
        return f".tar.{self.value}"


# gzip level 6 and xz preset 6: the usual defaults of the command-line tools.
# tarfile's own gzip default (9) costs much more CPU for little gain.
_COMPRESSION_LEVEL = 6


class OutputError(TreeAuditError):
    """The archive directory or the temporary archive is not what was expected."""


def check_file_name(name: str) -> None:
    """Refuse anything but a plain file name inside the archive directory."""
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\x00" in name
        or len(os.fsencode(name)) > 255
    ):
        raise OutputError(f"invalid archive file name: {name!r}")


class ArchiveOutput:
    """Writes one archive in the archive directory, then publishes or discards it."""

    def __init__(self, archive_dir: str) -> None:
        self.archive_dir = archive_dir
        self._dir_fd: int | None = None
        self._partial_name: str | None = None
        self._partial_id: tuple[int, int] | None = None
        self._file: BinaryIO | None = None
        self._compressor: io.BufferedIOBase | None = None

    # --- directory --------------------------------------------------------

    def __enter__(self) -> ArchiveOutput:
        self._dir_fd = os.open(self.archive_dir, DIR_FLAGS)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close_file()
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None

    @property
    def dir_fd(self) -> int:
        if self._dir_fd is None:
            raise OutputError("archive directory is not open")
        return self._dir_fd

    def directory_identity(self) -> tuple[int, int]:
        st = os.fstat(self.dir_fd)
        return (st.st_dev, st.st_ino)

    def free_bytes(self) -> int:
        """Space available to this (non-root) user in the archive directory."""
        st = os.fstatvfs(self.dir_fd)
        return st.f_bavail * st.f_frsize

    # --- temporary archive ------------------------------------------------

    def create(self, partial_name: str) -> BinaryIO:
        """Create the temporary archive file; it must not exist yet."""
        check_file_name(partial_name)
        if self._partial_name is not None:
            raise OutputError("a temporary archive was already created")
        fd = os.open(partial_name, CREATE_FLAGS, ARCHIVE_MODE, dir_fd=self.dir_fd)
        st = os.fstat(fd)
        self._partial_name = partial_name
        self._partial_id = (st.st_dev, st.st_ino)
        self._file = os.fdopen(fd, "wb", buffering=WRITE_BUFFER)
        return self._file

    def open_tar(self, partial_name: str, compression: Compression) -> TarStream:
        """Create the temporary archive and return a tar writer into it."""
        raw = self.create(partial_name)
        compressor: io.BufferedIOBase
        if compression is Compression.GZ:
            compressor = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=_COMPRESSION_LEVEL)
        else:
            # Closed by sync() or close_file(), like the gzip one.
            compressor = lzma.LZMAFile(raw, mode="wb", preset=_COMPRESSION_LEVEL)  # noqa: SIM115
        self._compressor = compressor
        return TarStream(compressor)

    def sync(self) -> int:
        """Finish the compressed stream, flush the archive to the disk; return its size.

        The TarStream must have been closed first (it writes the end-of-archive blocks).
        """
        if self._file is None:
            raise OutputError("no temporary archive is open")
        self._close_compressor()  # writes the gzip or xz trailer
        self._file.flush()
        os.fsync(self._file.fileno())
        return os.fstat(self._file.fileno()).st_size

    def _close_compressor(self) -> None:
        if self._compressor is not None:
            compressor, self._compressor = self._compressor, None
            compressor.close()  # does not close the underlying file

    def close_file(self) -> None:
        try:
            self._close_compressor()
        finally:
            if self._file is not None:
                file, self._file = self._file, None
                file.close()

    def open_partial_for_reading(self) -> int:
        """Reopen the temporary archive read-only, checking it is still ours."""
        fd = os.open(self._require_partial(), READ_FLAGS, dir_fd=self.dir_fd)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != self._partial_id or not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise OutputError("the temporary archive was replaced")
        return fd

    def _require_partial(self) -> str:
        if self._partial_name is None:
            raise OutputError("no temporary archive was created")
        return self._partial_name

    def _partial_is_ours(self) -> bool:
        try:
            st = os.stat(self._require_partial(), dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (st.st_dev, st.st_ino) == self._partial_id and stat.S_ISREG(st.st_mode)

    # --- publication ------------------------------------------------------

    def publish(self, final_name: str) -> str:
        """Give the verified archive its final name, never replacing a file.

        If ``final_name`` exists, "-2", "-3"... is inserted before the suffix.
        Returns the name actually used.
        """
        partial = self._require_partial()
        self.close_file()
        if not self._partial_is_ours():
            raise OutputError("the temporary archive was replaced")
        stem, suffix = _split_suffix(final_name)
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            name = final_name if attempt == 1 else f"{stem}-{attempt}{suffix}"
            check_file_name(name)
            if self._publish_as(partial, name):
                os.fsync(self.dir_fd)
                self._partial_name = None
                return name
        raise OutputError("no free archive name found")

    def _publish_as(self, partial: str, name: str) -> bool:
        """Try one final name; False if a file with that name already exists."""
        try:
            # link(2) fails if the target exists: an atomic "rename without replacing".
            os.link(
                partial,
                name,
                src_dir_fd=self.dir_fd,
                dst_dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno not in _NO_LINK_ERRNOS:
                raise
            if not self._rename_if_free(partial, name):
                return False
            self._check_published(name)
            self._partial_name = None
            return True
        self._check_published(name)
        # Both names now point to the archive: drop the temporary one, if it is
        # still ours (otherwise it is left alone).
        if self._partial_is_ours():
            self._remove_partial()
        else:
            self._partial_name = None
        return True

    def _check_published(self, name: str) -> None:
        """The final name must designate the archive this object wrote."""
        st = os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) != self._partial_id or not stat.S_ISREG(st.st_mode):
            raise OutputError("the published name does not designate the verified archive")

    def _rename_if_free(self, partial: str, name: str) -> bool:
        """Fallback without hard links: rename after checking the name is free.

        A file created under that name between the check and the rename by
        another program of the same user would be replaced: an accepted limit
        (outside the threat model, docs/SECURITY.md), only on such filesystems.
        """
        try:
            os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(partial, name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
            return True
        return False

    # --- removal of our own temporary file ----------------------------------

    def discard(self) -> bool:
        """Remove the temporary archive after a failure or a cancellation.

        The only removal in TreeAudit (D-032): it targets nothing but the file
        this object created, relative to the archive directory descriptor, and
        only if it is still the same inode. Returns True if it was removed.

        It must work when writing failed because the disk is full: closing the
        file then fails again (the buffered data cannot be written), which must
        not prevent the removal that frees the space.
        """
        try:
            self.close_file()
        except OSError as exc:
            logger.warning("temporary archive could not be closed cleanly: %s", exc.strerror)
        if self._partial_name is None or self._dir_fd is None:
            return False
        if not self._partial_is_ours():
            return False
        self._remove_partial()
        return True

    def _remove_partial(self) -> None:
        os.unlink(self._require_partial(), dir_fd=self.dir_fd)
        self._partial_name = None


class Content(Protocol):
    def read(self, size: int, /) -> bytes: ...


class TarStream:
    """Minimal tar writer built on the documented ``TarInfo.tobuf()``.

    PAX format: arbitrary name lengths and names that are not valid UTF-8
    (stored as raw bytes, D-014). Unlike ``TarFile``, it keeps nothing in
    memory per member and copies content by 1 MiB blocks.
    """

    def __init__(self, out: io.BufferedIOBase) -> None:
        self._out = out
        self._offset = 0
        self._closed = False

    def __enter__(self) -> TarStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()

    def _write(self, data: bytes) -> None:
        self._out.write(data)
        self._offset += len(data)

    def add(self, info: tarfile.TarInfo, content: Content | None = None) -> None:
        """Write one member; ``content`` must provide exactly ``info.size`` bytes."""
        if self._closed:
            raise OutputError("the tar stream is closed")
        self._write(info.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape"))
        if not info.isreg():
            return
        if content is None:
            raise OutputError("a regular member needs its content")
        remaining = info.size
        while remaining > 0:
            block = content.read(min(WRITE_BUFFER, remaining))
            if not block or len(block) > remaining:
                raise OutputError("content does not match the declared size")
            self._write(block)
            remaining -= len(block)
        padding = -info.size % tarfile.BLOCKSIZE
        if padding:
            self._write(bytes(padding))

    def close(self) -> None:
        """End of archive: two zero blocks, then padding to a whole record."""
        if self._closed:
            return
        self._write(bytes(2 * tarfile.BLOCKSIZE))
        padding = -self._offset % tarfile.RECORDSIZE
        if padding:
            self._write(bytes(padding))
        self._closed = True


def _split_suffix(name: str) -> tuple[str, str]:
    for compression in Compression:
        if name.endswith(compression.suffix):
            return name[: -len(compression.suffix)], compression.suffix
    return name, ""
