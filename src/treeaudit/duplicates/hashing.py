"""Content digests and byte comparison (D-015). Memory use is bounded by BLOCK_SIZE."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable

PART_SIZE = 64 * 1024
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
    """Small files are read entirely by the partial digest."""
    return size <= 2 * PART_SIZE


def partial_digest(fd: int, size: int) -> tuple[bytes, int]:
    """Digest of the first and last PART_SIZE bytes (of everything if small).

    Returns the digest and the number of bytes read.
    """
    digest = hashlib.sha256()
    if is_small(size):
        data = _read_exactly(fd, size, 0)
        digest.update(data)
        return digest.digest(), len(data)
    head = _read_exactly(fd, PART_SIZE, 0)
    tail = _read_exactly(fd, PART_SIZE, size - PART_SIZE)
    digest.update(head)
    digest.update(tail)
    return digest.digest(), 2 * PART_SIZE


def full_digest(
    fd: int, size: int, *, cancel: threading.Event, on_read: Callable[[int], None]
) -> bytes:
    """SHA-256 of the whole file, read block by block; checks cancellation."""
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        if cancel.is_set():
            raise CancelledError
        block = _read_exactly(fd, min(BLOCK_SIZE, size - offset), offset)
        digest.update(block)
        offset += len(block)
        on_read(len(block))
    return digest.digest()


def same_content(
    fd_a: int, fd_b: int, size: int, *, cancel: threading.Event, on_read: Callable[[int], None]
) -> bool:
    """Byte-by-byte comparison of two files of the same size."""
    offset = 0
    while offset < size:
        if cancel.is_set():
            raise CancelledError
        length = min(BLOCK_SIZE, size - offset)
        if _read_exactly(fd_a, length, offset) != _read_exactly(fd_b, length, offset):
            return False
        offset += length
        on_read(2 * length)
    return True
