"""Content digests and byte comparison (D-015). Memory use is bounded by BLOCK_SIZE.

Stages, each reading as little as possible (one seek for the head, one for the
tail, then a sequential read): files that differ are usually told apart by
their first or last 4 KiB without reading the rest.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from collections.abc import Callable

EDGE_SIZE = 4096
"""Bytes read at the start (head) and at the end (tail) of a file."""
BLOCK_SIZE = 1024 * 1024
ALGORITHM = "sha256"


class CancelledError(Exception):
    """The analysis was cancelled while a file was being read."""


def _read_exactly(fd: int, length: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    while length > 0:
        chunk = os.pread(fd, length, offset)
        if not chunk:
            raise OSError(0, "file shorter than expected")
        chunks.append(chunk)
        length -= len(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def is_small(size: int) -> bool:
    """Small files are read entirely at the head stage (one read)."""
    return size <= 2 * EDGE_SIZE


def _advise(fd: int, advice: int) -> None:
    # Advice only: some filesystems refuse it, and reading works the same.
    with contextlib.suppress(OSError):
        os.posix_fadvise(fd, 0, 0, advice)


def head_digest(fd: int, size: int) -> tuple[bytes, int]:
    """Digest of the first EDGE_SIZE bytes (of the whole file if small), bytes read."""
    data = _read_exactly(fd, size if is_small(size) else EDGE_SIZE, 0)
    return hashlib.sha256(data).digest(), len(data)


def tail_digest(fd: int, size: int) -> tuple[bytes, int]:
    data = _read_exactly(fd, EDGE_SIZE, size - EDGE_SIZE)
    return hashlib.sha256(data).digest(), len(data)


def full_digest(
    fd: int, size: int, *, cancel: threading.Event, on_read: Callable[[int], None]
) -> bytes:
    """SHA-256 of the whole file, read sequentially block by block.

    The kernel is told the read is sequential (larger read-ahead) and, once done,
    that the pages are not needed anymore, so a large analysis does not push
    the rest of the machine's memory out of the page cache.
    """
    _advise(fd, os.POSIX_FADV_SEQUENTIAL)
    digest = hashlib.sha256()
    offset = 0
    try:
        while offset < size:
            if cancel.is_set():
                raise CancelledError
            block = _read_exactly(fd, min(BLOCK_SIZE, size - offset), offset)
            digest.update(block)
            offset += len(block)
            on_read(len(block))
    finally:
        _advise(fd, os.POSIX_FADV_DONTNEED)
    return digest.digest()


def same_content(
    fd_a: int, fd_b: int, size: int, *, cancel: threading.Event, on_read: Callable[[int], None]
) -> bool:
    """Byte-by-byte comparison of two files of the same size."""
    offset = 0
    try:
        while offset < size:
            if cancel.is_set():
                raise CancelledError
            length = min(BLOCK_SIZE, size - offset)
            if _read_exactly(fd_a, length, offset) != _read_exactly(fd_b, length, offset):
                return False
            offset += length
            on_read(2 * length)
    finally:
        _advise(fd_a, os.POSIX_FADV_DONTNEED)
        _advise(fd_b, os.POSIX_FADV_DONTNEED)
    return True
