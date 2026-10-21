"""Reading a source file into the archive, hashing exactly what is archived.

tarfile writes a member header (with its size) before copying the content, so
the content must have exactly that size. If the file shrinks or a read fails
(bad sector on an old disk), the missing bytes are replaced by zeros to keep
the archive readable, and the failure is recorded: the caller then reports the
file and the archive cannot be ``complete``. Bytes added after the header was
written are not read; the caller detects the change with ``fstat``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from collections.abc import Callable

BLOCK_SIZE = 1024 * 1024


class CancelledError(Exception):
    """The archive was cancelled while a file was being read."""


class HashingReader:
    """Minimal file object for ``TarFile.addfile``: reads ``size`` bytes of ``fd``."""

    def __init__(
        self,
        fd: int,
        size: int,
        *,
        cancel: threading.Event,
        on_read: Callable[[int], None],
    ) -> None:
        self._fd = fd
        self._remaining = size
        self._cancel = cancel
        self._on_read = on_read
        self._buffer = b""
        self._position = 0
        self._digest = hashlib.sha256()
        self.failure: str | None = None
        """Why the content is not the file's real content, if it is not."""
        # Advice only pays for files read in several blocks: skip two system
        # calls per file on trees of small files.
        self._advise = size > BLOCK_SIZE
        if self._advise:
            with contextlib.suppress(OSError):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)

    def read(self, size: int = -1) -> bytes:
        if self._cancel.is_set():
            raise CancelledError
        if size < 0:
            size = self._remaining + len(self._buffer) - self._position
        parts: list[bytes] = []
        while size > 0:
            if self._position >= len(self._buffer):
                if self._remaining <= 0:
                    break
                self._buffer = self._next_block(max(BLOCK_SIZE, size))
                self._position = 0
            part = self._buffer[self._position : self._position + size]
            self._position += len(part)
            size -= len(part)
            parts.append(part)
        data = b"".join(parts)
        self._digest.update(data)
        return data

    def _next_block(self, length: int) -> bytes:
        length = min(length, self._remaining)
        block = b""
        if self.failure is None:
            try:
                block = os.read(self._fd, length)
            except OSError as exc:
                self.failure = f"erreur de lecture : {exc.strerror or exc}"
            else:
                if not block:
                    self.failure = "le fichier a raccourci pendant la lecture"
        if not block:
            block = bytes(length)  # keep the declared size, see the module docstring
        self._remaining -= len(block)
        self._on_read(len(block))
        return block

    def digest(self) -> bytes:
        """SHA-256 of every byte handed out so far."""
        return self._digest.digest()

    def done(self) -> None:
        """Tell the kernel the file's pages are no longer needed (8 GiB machines)."""
        if not self._advise:
            return
        with contextlib.suppress(OSError):
            os.posix_fadvise(self._fd, 0, 0, os.POSIX_FADV_DONTNEED)
