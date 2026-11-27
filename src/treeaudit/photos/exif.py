"""The date a photo was taken, read from its EXIF metadata (D-071).

Pure Python and the standard library only: nothing is decoded, no image
library parses the file. The file is untrusted, so every read goes through a
budget (``MAX_READ`` bytes per file), every loop has a fixed bound, and any
malformed structure gives "no date", never an exception.

Formats: JPEG (APP1 segment), TIFF and the raw formats built on it (DNG, NEF,
CR2, ARW, ORF, RW2, PEF...), HEIF, HEIC and AVIF (the "Exif" item), PNG
(eXIf chunk) and WebP (EXIF chunk). Only DateTimeOriginal, else
DateTimeDigitized, is taken: DateTime (0x0132) is the date of the last edit.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from datetime import datetime

Reader = Callable[[int, int], bytes]
"""read(offset, size) -> at most size bytes from offset (fewer at the end)."""

MAX_READ = 256 * 1024
"""Bytes read from one file at most, all reads together."""
EXTENSIONS = frozenset(
    {
        "jpg", "jpeg", "jpe", "jfif",
        "tif", "tiff", "dng", "nef", "nrw", "cr2", "arw", "srf", "sr2", "orf", "rw2", "pef",
        "heic", "heif", "hif", "avif",
        "png", "webp",
    }
)  # fmt: skip
"""Extensions worth reading (lower case, without the dot)."""

_DATE_ORIGINAL = 0x9003
_DATE_DIGITIZED = 0x9004
_EXIF_IFD = 0x8769
_ASCII = 2
_LONG = 4
_MAX_ENTRIES = 512
"""Entries of one IFD beyond which it is treated as damaged."""
_MAX_STEPS = 256
"""Segments, chunks or boxes walked at most in one file."""
_TIFF_MAGIC = {b"II*\x00", b"MM\x00*", b"IIRO", b"MMOR", b"IIU\x00"}
"""TIFF, and the headers of Olympus ORF and Panasonic RW2 built like it."""


class _Damaged(Exception):
    """The file does not hold what its structure says, or the budget is spent."""


class _Budget:
    """Reads from the file, at most MAX_READ bytes all together."""

    def __init__(self, read: Reader, size: int) -> None:
        self._read = read
        self.size = size
        self._left = MAX_READ

    def get(self, offset: int, length: int) -> bytes:
        """Exactly ``length`` bytes at ``offset``, or _Damaged."""
        if offset < 0 or length < 0 or offset + length > self.size or length > self._left:
            raise _Damaged
        self._left -= length
        data = self._read(offset, length)
        if len(data) != length:
            raise _Damaged
        return data


def taken_at(read: Reader, size: int) -> datetime | None:
    """When the photo was taken, as the camera wrote it (its own clock, no
    time zone); None when the file has no such date or is damaged."""
    budget = _Budget(read, size)
    try:
        head = budget.get(0, min(16, size))
        if head.startswith(b"\xff\xd8"):
            return _from_jpeg(budget)
        if head[:4] in _TIFF_MAGIC:
            return _from_tiff(budget, 0, budget.size)
        if head[4:8] == b"ftyp":
            return _from_heif(budget)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return _from_png(budget)
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _from_webp(budget)
    except (_Damaged, struct.error, ValueError, OverflowError):
        return None
    return None


def _strip_exif_prefix(budget: _Budget, offset: int, end: int) -> int:
    """Some writers put "Exif\\0\\0" before the TIFF header: skip it."""
    if end - offset >= 6 and budget.get(offset, 6) == b"Exif\x00\x00":
        return offset + 6
    return offset


def _from_jpeg(budget: _Budget) -> datetime | None:
    """Walk the segments up to the image data; the first APP1 "Exif" holds it."""
    offset = 2
    for _ in range(_MAX_STEPS):
        marker = budget.get(offset, 4)
        if marker[0] != 0xFF:
            return None
        kind = marker[1]
        if kind == 0xFF:  # fill byte
            offset += 1
            continue
        if kind in (0xD9, 0xDA):  # end of image, start of scan: no metadata after
            return None
        if 0xD0 <= kind <= 0xD7 or kind == 0x01:  # markers without a length
            offset += 2
            continue
        (length,) = struct.unpack(">H", marker[2:4])
        if length < 2:
            return None
        if kind == 0xE1 and length >= 8 and budget.get(offset + 4, 6) == b"Exif\x00\x00":
            return _from_tiff(budget, offset + 10, offset + 2 + length)
        offset += 2 + length
    return None


def _from_tiff(budget: _Budget, base: int, end: int) -> datetime | None:
    """A TIFF structure from ``base`` to ``end``: IFD0, then its Exif IFD."""
    end = min(end, budget.size)
    if end - base < 8:
        return None
    header = budget.get(base, 8)
    order = {b"II": "<", b"MM": ">"}.get(header[:2])
    if order is None:
        return None
    (first,) = struct.unpack(order + "I", header[4:8])
    entries = _ifd(budget, base, end, first, order)
    exif = entries.get(_EXIF_IFD)
    if exif is None or exif[0] not in (_LONG, 13):  # LONG, or IFD
        return None
    (pointer,) = struct.unpack(order + "I", exif[2])
    found = _ifd(budget, base, end, pointer, order)
    for tag in (_DATE_ORIGINAL, _DATE_DIGITIZED):
        value = _ascii(budget, base, end, found.get(tag), order)
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _ifd(
    budget: _Budget, base: int, end: int, offset: int, order: str
) -> dict[int, tuple[int, int, bytes]]:
    """Tag -> (type, count, the 4 bytes of value or offset) of one IFD."""
    if offset < 8 or base + offset + 2 > end:
        raise _Damaged
    (count,) = struct.unpack(order + "H", budget.get(base + offset, 2))
    if count > _MAX_ENTRIES or base + offset + 2 + 12 * count > end:
        raise _Damaged
    raw = budget.get(base + offset + 2, 12 * count)
    entries: dict[int, tuple[int, int, bytes]] = {}
    for index in range(count):
        tag, kind, number = struct.unpack(order + "HHI", raw[12 * index : 12 * index + 8])
        entries.setdefault(tag, (kind, number, raw[12 * index + 8 : 12 * index + 12]))
    return entries


def _ascii(
    budget: _Budget, base: int, end: int, entry: tuple[int, int, bytes] | None, order: str
) -> bytes | None:
    """The text of an ASCII entry, read where it is (in the entry, or at its offset)."""
    if entry is None or entry[0] != _ASCII or not 0 < entry[1] <= 64:
        return None
    count, value = entry[1], entry[2]
    if count <= 4:
        return value[:count]
    (pointer,) = struct.unpack(order + "I", value)
    if base + pointer + count > end:
        return None
    return budget.get(base + pointer, count)


def _parse_date(value: bytes | None) -> datetime | None:
    """ "2023:07:14 10:22:31" (the separators vary); None for "0000:00:00..."
    or anything that is not a real date."""
    if value is None:
        return None
    text = value.split(b"\x00", 1)[0].strip()
    if len(text) < 19 or not text[:4].isdigit():
        return None
    digits = [text[0:4], text[5:7], text[8:10], text[11:13], text[14:16], text[17:19]]
    if not all(part.isdigit() for part in digits):
        return None
    try:
        year, month, day, hour, minute, second = (int(part) for part in digits)
        return datetime(year, month, day, hour, minute, second)  # the camera's clock, no zone
    except ValueError:
        return None


def _boxes(budget: _Budget, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """ISO base media boxes between ``start`` and ``end``: (type, content start, end)."""
    found: list[tuple[bytes, int, int]] = []
    offset = start
    while offset + 8 <= end and len(found) < _MAX_STEPS:
        size, kind = struct.unpack(">I4s", budget.get(offset, 8))
        header = 8
        if size == 1:
            (size,) = struct.unpack(">Q", budget.get(offset + 8, 8))
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise _Damaged
        found.append((kind, offset + header, offset + size))
        offset += size
    return found


def _from_heif(budget: _Budget) -> datetime | None:
    """HEIF/HEIC/AVIF: the "Exif" item, found in iinf, located by iloc."""
    meta = next((b for b in _boxes(budget, 0, budget.size) if b[0] == b"meta"), None)
    if meta is None:
        return None
    children = _boxes(budget, meta[1] + 4, meta[2])  # meta is a full box
    by_kind = {kind: (start, end) for kind, start, end in children}
    if b"iinf" not in by_kind or b"iloc" not in by_kind:
        return None
    item = _exif_item(budget, *by_kind[b"iinf"])
    if item is None:
        return None
    extent = _item_extent(budget, *by_kind[b"iloc"], item, by_kind.get(b"idat"))
    if extent is None:
        return None
    start, length = extent
    if length < 4:
        return None
    # The item starts with the offset of the TIFF header after these 4 bytes.
    (skip,) = struct.unpack(">I", budget.get(start, 4))
    tiff = start + 4 + skip
    tiff = _strip_exif_prefix(budget, tiff, start + length)
    return _from_tiff(budget, tiff, start + length)


def _exif_item(budget: _Budget, start: int, end: int) -> int | None:
    """The id of the item of type "Exif" in an iinf box."""
    version = budget.get(start, 1)[0]
    count_size = 2 if version == 0 else 4
    offset = start + 4 + count_size
    for kind, content, _box_end in _boxes(budget, offset, end):
        if kind != b"infe":
            continue
        infe_version = budget.get(content, 1)[0]
        if infe_version < 2:
            continue
        id_size = 2 if infe_version == 2 else 4
        fields = budget.get(content + 4, id_size + 2 + 4)
        item_id = int.from_bytes(fields[:id_size], "big")
        if fields[id_size + 2 :] == b"Exif":
            return item_id
    return None


def _item_extent(
    budget: _Budget, start: int, end: int, wanted: int, idat: tuple[int, int] | None
) -> tuple[int, int] | None:
    """(file offset, length) of an item's first extent, from an iloc box."""
    version = budget.get(start, 1)[0]
    if version > 2:
        return None
    sizes = budget.get(start + 4, 2)
    offset_size, length_size = sizes[0] >> 4, sizes[0] & 15
    base_size = sizes[1] >> 4
    index_size = sizes[1] & 15 if version in (1, 2) else 0
    offset = start + 6
    id_size = 4 if version == 2 else 2
    count = int.from_bytes(budget.get(offset, id_size), "big")
    offset += id_size

    def number(width: int) -> int:
        nonlocal offset
        if width not in (0, 2, 4, 8):
            raise _Damaged
        value = int.from_bytes(budget.get(offset, width), "big") if width else 0
        offset += width
        return value

    for _ in range(min(count, _MAX_STEPS)):
        item_id = number(id_size)
        method = 0
        if version in (1, 2):
            method = number(2) & 15
        number(2)  # data reference index
        base = number(base_size)
        extents = number(2)
        if extents == 0 or extents > _MAX_STEPS:
            raise _Damaged
        first: tuple[int, int] | None = None
        for _ in range(extents):
            number(index_size)
            extent_offset, extent_length = number(offset_size), number(length_size)
            if first is None:
                first = (base + extent_offset, extent_length)
        if item_id != wanted or first is None:
            continue
        if method == 0:
            return first
        if method == 1 and idat is not None:
            return (idat[0] + first[0], first[1])
        return None  # construction method 2 (from other items) is not used for Exif
    if offset > end:
        raise _Damaged
    return None


def _from_png(budget: _Budget) -> datetime | None:
    """PNG: the eXIf chunk, before the image data."""
    offset = 8
    for _ in range(_MAX_STEPS):
        length, kind = struct.unpack(">I4s", budget.get(offset, 8))
        if kind == b"eXIf":
            start = _strip_exif_prefix(budget, offset + 8, offset + 8 + length)
            return _from_tiff(budget, start, offset + 8 + length)
        if kind in (b"IDAT", b"IEND"):
            return None
        offset += 12 + length
    return None


def _from_webp(budget: _Budget) -> datetime | None:
    """WebP: the EXIF chunk of the RIFF container."""
    offset = 12
    for _ in range(_MAX_STEPS):
        kind, length = struct.unpack("<4sI", budget.get(offset, 8))
        if kind == b"EXIF":
            start = _strip_exif_prefix(budget, offset + 8, offset + 8 + length)
            return _from_tiff(budget, start, offset + 8 + length)
        offset += 8 + length + (length & 1)
    return None
