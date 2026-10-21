"""Verification of a written archive against what was read (D-024, D-032).

While writing, each member is summarised by a 16-byte fingerprint of its type,
name, permissions, size, link target and content SHA-256 (``Manifest``). The
archive is then read back from the disk, decompressed, and every member must
give the same fingerprint, in the same order, with no extra member; reading
the compressed stream to its end also checks its own checksum (gzip CRC-32,
xz CRC-64). Memory stays bounded: 16 bytes per member, 16 MB per million.

Nothing is extracted: members are only read in memory, block by block.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import os
import re
import stat
import struct
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

from treeaudit.archive.output import Compression
from treeaudit.archive.source import BLOCK_SIZE, CancelledError

FINGERPRINT_SIZE = 16

KIND_DIR = b"d"
KIND_FILE = b"f"
KIND_SYMLINK = b"l"

# Lengths of the variable fields first, then mode and size: no two different
# members can produce the same input.
_FIXED = struct.Struct("=QQQQQQ")


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def printable(raw: bytes) -> str:
    """One-line printable form of a raw name: invalid UTF-8 and control characters
    appear as \\xNN escapes (D-014)."""
    text = raw.decode("utf-8", errors="backslashreplace")
    return _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def fingerprint(
    kind: bytes, name: bytes, mode: int, size: int, link: bytes, content: bytes
) -> bytes:
    """Fingerprint of one member; every field is length-prefixed (no ambiguity)."""
    fixed = _FIXED.pack(len(kind), len(name), len(link), len(content), stat.S_IMODE(mode), size)
    return hashlib.sha256(fixed + kind + name + link + content).digest()[:FINGERPRINT_SIZE]


class Manifest:
    """Fingerprints of the members written, in order."""

    def __init__(self) -> None:
        self._data = bytearray()

    def add(self, value: bytes) -> None:
        self._data += value

    def __len__(self) -> int:
        return len(self._data) // FINGERPRINT_SIZE

    def get(self, index: int) -> bytes:
        return bytes(self._data[index * FINGERPRINT_SIZE : (index + 1) * FINGERPRINT_SIZE])


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    members: int
    problem: str | None = None
    """Why the archive does not match (French, shown to the user)."""


def _forget_members(tar: tarfile.TarFile) -> None:
    # ``members`` is not part of the documented API: if it ever disappears,
    # only the memory use grows.
    members = getattr(tar, "members", None)
    if isinstance(members, list):
        members.clear()


def _decompressor(raw: BinaryIO, compression: Compression) -> io.BufferedIOBase:
    if compression is Compression.GZ:
        return gzip.GzipFile(fileobj=raw, mode="rb")
    return lzma.LZMAFile(raw, mode="rb")


def _member_fingerprint(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    cancel: threading.Event,
    on_read: Callable[[int], None],
) -> bytes | None:
    """Fingerprint of a member read back, or None for an unexpected type."""
    name = os.fsencode(member.name)
    if member.isdir():
        return fingerprint(KIND_DIR, name, member.mode, 0, b"", b"")
    if member.issym():
        link = os.fsencode(member.linkname)
        return fingerprint(KIND_SYMLINK, name, member.mode, 0, link, b"")
    if not member.isreg():
        return None
    content = tar.extractfile(member)
    if content is None:  # pragma: no cover - a regular member always has content
        return None
    digest = hashlib.sha256()
    while True:
        if cancel.is_set():
            raise CancelledError
        block = content.read(BLOCK_SIZE)
        if not block:
            break
        digest.update(block)
        on_read(len(block))
    return fingerprint(KIND_FILE, name, member.mode, member.size, b"", digest.digest())


def verify_archive(
    fd: int,
    compression: Compression,
    manifest: Manifest,
    *,
    cancel: threading.Event,
    on_read: Callable[[int], None],
) -> VerifyResult:
    """Read the archive at ``fd`` back and compare it with ``manifest``.

    Raises CancelledError if cancelled. Any read or format error is a failed
    verification, never an exception: the archive is then not trusted.
    """
    count = 0
    try:
        with (
            os.fdopen(fd, "rb", closefd=False) as raw,
            _decompressor(raw, compression) as stream,
            tarfile.open(
                fileobj=stream, mode="r|", encoding="utf-8", errors="surrogateescape"
            ) as tar,
        ):
            # tar.next() rather than iterating: TarFile keeps every member it reads,
            # even in stream mode (about 500 MB for a million); they are dropped
            # as soon as they are checked.
            while (member := tar.next()) is not None:
                _forget_members(tar)
                if count >= len(manifest):
                    return VerifyResult(False, count, "l'archive contient des éléments en trop")
                value = _member_fingerprint(tar, member, cancel=cancel, on_read=on_read)
                if value != manifest.get(count):
                    return VerifyResult(
                        False,
                        count,
                        f"l'élément n° {count + 1} ({printable(os.fsencode(member.name))}) "
                        "ne correspond pas à ce qui a été lu",
                    )
                count += 1
            # Read the compressed stream to its very end: this checks its checksum
            # and that nothing follows the end of the tar archive.
            while block := stream.read(BLOCK_SIZE):
                if block.strip(b"\x00"):
                    return VerifyResult(False, count, "données inattendues après l'archive")
    except CancelledError:
        raise
    except (OSError, EOFError, tarfile.TarError, lzma.LZMAError) as exc:
        return VerifyResult(False, count, f"archive illisible : {exc}")
    if count != len(manifest):
        return VerifyResult(
            False, count, f"{len(manifest) - count} élément(s) manquant(s) dans l'archive"
        )
    return VerifyResult(True, count)
