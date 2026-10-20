"""Safe reading of files of a root, designated by their path relative to the root.

Every directory on the way is opened relative to its parent with O_NOFOLLOW,
starting from a descriptor of the root, so a symlink swapped in since the scan
can never lead outside the root. A file must still be the one recorded in the
index (same device, inode, size and modification time) and a regular file:
O_NONBLOCK guarantees that a FIFO swapped in its place cannot block the reader.

Only the directories of the current path stay open (like the walker), so
reading files in path order reuses descriptors and keeps their number bounded.
"""

from __future__ import annotations

import errno as errno_codes
import os
import stat
from dataclasses import dataclass
from types import TracebackType

from treeaudit.core.errors import TreeAuditError

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NOATIME = getattr(os, "O_NOATIME", 0)


class RootChangedError(TreeAuditError):
    """The root is no longer the directory that was scanned."""


class FileChangedError(Exception):
    """The file differs from the index (replaced, modified or not a regular file)."""


@dataclass(frozen=True, slots=True)
class Expected:
    """Identity of a file as recorded by the scan."""

    dev: int
    ino: int
    size: int
    mtime_ns: int

    def matches(self, st: os.stat_result) -> bool:
        return (
            stat.S_ISREG(st.st_mode)
            and st.st_dev == self.dev
            and st.st_ino == self.ino
            and st.st_size == self.size
            and st.st_mtime_ns == self.mtime_ns
        )


def _open(name: bytes | str, flags: int, dir_fd: int | None) -> int:
    """Open with O_NOATIME when the kernel allows it (owner only)."""
    if _NOATIME:
        try:
            return os.open(name, flags | _NOATIME, dir_fd=dir_fd)
        except PermissionError as exc:
            if exc.errno != errno_codes.EPERM:
                raise
    return os.open(name, flags, dir_fd=dir_fd)


class TreeReader:
    """Opens files below a root without ever following a symlink."""

    def __init__(self, root_path: str, expected_root: tuple[int, int]) -> None:
        self.root_path = root_path
        self.expected_root = expected_root
        # (component, fd) from the root (component b"") down to the current dir.
        self._stack: list[tuple[bytes, int]] = []

    def __enter__(self) -> TreeReader:
        fd = _open(self.root_path, DIR_FLAGS, None)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != self.expected_root:
            os.close(fd)
            raise RootChangedError(f"{self.root_path} is not the scanned directory anymore")
        self._stack = [(b"", fd)]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._truncate(0)

    @property
    def open_descriptors(self) -> int:
        return len(self._stack)

    def _truncate(self, keep: int) -> None:
        while len(self._stack) > keep:
            _, fd = self._stack.pop()
            os.close(fd)

    def _directory(self, components: list[bytes]) -> int:
        """Descriptor of the directory reached through ``components`` from the root."""
        common = 0
        while (
            common < len(components)
            and common + 1 < len(self._stack)
            and self._stack[common + 1][0] == components[common]
        ):
            common += 1
        self._truncate(common + 1)
        for component in components[common:]:
            fd = _open(component, DIR_FLAGS, self._stack[-1][1])
            self._stack.append((component, fd))
        return self._stack[-1][1]

    def open_file(self, rel_path: bytes, expected: Expected) -> int:
        """Open a regular file of the root for reading; the caller closes it.

        Raises OSError if a component cannot be opened (missing, permission,
        replaced by a symlink) and FileChangedError if the file is not the one
        recorded in the index.
        """
        *directories, name = rel_path.split(b"/")
        if not name or b"" in directories:
            raise FileChangedError("invalid relative path")
        try:
            parent = self._directory(directories)
        except OSError:
            self._truncate(1)  # forget a partially opened chain
            raise
        fd = _open(name, FILE_FLAGS, parent)
        if not expected.matches(os.fstat(fd)):
            os.close(fd)
            raise FileChangedError("file differs from the scan")
        return fd


def check_unchanged(fd: int, expected: Expected) -> None:
    """Raise FileChangedError if the open file changed while it was being read."""
    if not expected.matches(os.fstat(fd)):
        raise FileChangedError("file modified while it was being read")
