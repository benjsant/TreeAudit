"""The date a photo was taken, read from EXIF (D-071), on files built byte by byte."""

from __future__ import annotations

import random
import struct
from datetime import datetime

import pytest

from treeaudit.photos import exif

TAKEN = datetime(2023, 7, 14, 10, 22, 31)
DATE = b"2023:07:14 10:22:31\x00"


def tiff(
    order: str = "<",
    tags: dict[int, bytes] | None = None,
    *,
    ifd0_extra: tuple[tuple[int, int, int, bytes], ...] = (),
) -> bytes:
    """A TIFF structure: IFD0 with an Exif IFD pointer, the Exif IFD with
    ASCII tags (value stored after the IFDs)."""
    tags = {0x9003: DATE} if tags is None else tags
    mark = b"II" if order == "<" else b"MM"
    header = mark + struct.pack(order + "HI", 42, 8)
    ifd0_entries = [(0x8769, 4, 1, b""), *ifd0_extra]
    ifd0_size = 2 + 12 * len(ifd0_entries) + 4
    exif_offset = 8 + ifd0_size
    exif_size = 2 + 12 * len(tags) + 4
    data_offset = exif_offset + exif_size
    ifd0 = struct.pack(order + "H", len(ifd0_entries))
    for tag, kind, count, value in ifd0_entries:
        raw = struct.pack(order + "I", exif_offset) if tag == 0x8769 else value.ljust(4, b"\0")
        ifd0 += struct.pack(order + "HHI", tag, kind, count) + raw
    ifd0 += b"\0\0\0\0"
    body, data = struct.pack(order + "H", len(tags)), b""
    for tag, text in sorted(tags.items()):
        if len(text) <= 4:
            value = text.ljust(4, b"\0")
        else:
            value = struct.pack(order + "I", data_offset + len(data))
            data += text
        body += struct.pack(order + "HHI", tag, 2, len(text)) + value
    body += b"\0\0\0\0"
    return header + ifd0 + body + data


def jpeg(payload: bytes, *, before: bytes = b"") -> bytes:
    app1 = b"Exif\0\0" + payload
    segment = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\1\1\0\0\1\0\1\0\0"
    return b"\xff\xd8" + app0 + before + segment + b"\xff\xda\0\x08" + b"\0" * 64 + b"\xff\xd9"


def box(kind: bytes, content: bytes) -> bytes:
    return struct.pack(">I", 8 + len(content)) + kind + content


def full_box(kind: bytes, version: int, content: bytes) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + content)


def heif(payload: bytes, *, iloc_version: int = 0, in_idat: bool = False) -> bytes:
    """ftyp, meta (hdlr, iinf with an image and an Exif item, iloc), mdat."""
    item = struct.pack(">I", 6) + b"Exif\0\0" + payload
    infe_image = full_box(b"infe", 2, struct.pack(">HH", 1, 0) + b"hvc1")
    infe_exif = full_box(b"infe", 2, struct.pack(">HH", 2, 0) + b"Exif")
    iinf = full_box(b"iinf", 0, struct.pack(">H", 2) + infe_image + infe_exif)
    hdlr = full_box(b"hdlr", 0, b"\0\0\0\0pict" + b"\0" * 13)
    ftyp = box(b"ftyp", b"heic\0\0\0\0mif1heic")

    def build(exif_offset: int) -> bytes:
        sizes = bytes([0x44, 0x40])  # 4-byte offsets, lengths and base; no extent index
        count = struct.pack(">H", 2)
        entries = b""
        for item_id, offset, length in ((1, 0, 10), (2, exif_offset, len(item))):
            entries += struct.pack(">H", item_id)
            if iloc_version:  # construction method: 1 = inside idat
                entries += struct.pack(">H", 1 if in_idat and item_id == 2 else 0)
            entries += struct.pack(">HIH", 0, 0, 1) + struct.pack(">II", offset, length)
        iloc = full_box(b"iloc", iloc_version, sizes + count + entries)
        idat = box(b"idat", item) if in_idat else b""
        meta = full_box(b"meta", 0, hdlr + iinf + iloc + idat)
        return ftyp + meta

    head = build(0)  # every field has a fixed size: the same length for any offset
    if in_idat:
        return head + box(b"mdat", b"\0" * 10)
    mdat_content = b"\0" * 10 + item
    exif_at = len(head) + 8 + 10
    return build(exif_at) + box(b"mdat", mdat_content)


def png(payload: bytes, *, prefix: bytes = b"") -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + b"\0\0\0\0"

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + ihdr + chunk(b"eXIf", prefix + payload) + chunk(b"IDAT", b"x")


def webp(payload: bytes, *, prefix: bytes = b"") -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return kind + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")

    body = b"WEBP" + chunk(b"VP8X", b"\x08" + b"\0" * 9) + chunk(b"EXIF", prefix + payload)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class Recorder:
    """A file in memory that counts what is read from it."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read_bytes = 0

    def __call__(self, offset: int, size: int) -> bytes:
        self.read_bytes += size
        return self.data[offset : offset + size]


def taken(data: bytes) -> datetime | None:
    recorder = Recorder(data)
    found = exif.taken_at(recorder, len(data))
    assert recorder.read_bytes <= exif.MAX_READ
    return found


@pytest.mark.parametrize("order", ["<", ">"])
def test_every_format_gives_the_date_taken(order: str) -> None:
    payload = tiff(order)
    assert taken(payload) == TAKEN  # TIFF, DNG, NEF, CR2...
    assert taken(jpeg(payload)) == TAKEN
    assert taken(png(payload)) == TAKEN
    assert taken(png(payload, prefix=b"Exif\0\0")) == TAKEN
    assert taken(webp(payload)) == TAKEN
    assert taken(webp(payload, prefix=b"Exif\0\0")) == TAKEN
    assert taken(heif(payload)) == TAKEN
    assert taken(heif(payload, iloc_version=1)) == TAKEN
    assert taken(heif(payload, iloc_version=1, in_idat=True)) == TAKEN


def test_raw_headers_built_like_tiff() -> None:
    payload = tiff("<")
    assert taken(b"IIRO" + payload[4:]) == TAKEN  # Olympus ORF
    assert taken(b"IIU\0" + payload[4:]) == TAKEN  # Panasonic RW2


def test_a_jpeg_with_other_segments_first() -> None:
    xmp = b"http://ns.adobe.com/xap/1.0/\0" + b"<x/>" * 50
    other = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp  # APP1, not Exif
    icc = b"\xff\xe2" + struct.pack(">H", 102) + b"\0" * 100
    assert taken(jpeg(tiff(), before=other + icc)) == TAKEN


def test_the_digitized_date_when_the_original_is_missing() -> None:
    assert taken(jpeg(tiff(tags={0x9004: DATE}))) == TAKEN
    both = {0x9003: b"2023:07:14 10:22:31\0", 0x9004: b"2024:01:01 00:00:00\0"}
    assert taken(jpeg(tiff(tags=both))) == TAKEN


def test_the_date_of_the_last_edit_is_not_the_date_taken() -> None:
    edited = tiff(tags={}, ifd0_extra=((0x0132, 2, 4, b"20"),))
    assert taken(jpeg(edited)) is None
    assert taken(jpeg(tiff(tags={0x0132: DATE}))) is None


@pytest.mark.parametrize(
    "text",
    [
        b"0000:00:00 00:00:00\0",
        b"                   \0",
        b"2023:02:30 10:00:00\0",
        b"2023:07:14\0",
        b"abcd:07:14 10:22:31\0",
        b"\0" * 20,
    ],
)
def test_an_unset_or_impossible_date_is_none(text: bytes) -> None:
    assert taken(jpeg(tiff(tags={0x9003: text}))) is None


def test_other_separators_are_accepted() -> None:
    assert taken(jpeg(tiff(tags={0x9003: b"2023-07-14 10:22:31\0"}))) == TAKEN


def test_files_without_metadata() -> None:
    assert taken(b"") is None
    assert taken(b"\xff\xd8\xff\xda\0\x08" + b"\0" * 100) is None  # image data at once
    assert taken(b"plain text, named photo.jpg") is None
    assert taken(b"GIF89a" + b"\0" * 100) is None


def test_an_ifd_pointing_to_itself_ends() -> None:
    data = bytearray(tiff("<"))
    # The Exif IFD pointer now designates IFD0 itself: no date, no endless loop.
    data[8 + 2 + 8 : 8 + 2 + 12] = struct.pack("<I", 8)
    assert taken(bytes(data)) is None


def test_huge_counts_and_offsets_are_refused_without_reading_them() -> None:
    data = bytearray(tiff("<"))
    data[8:10] = struct.pack("<H", 0xFFFF)  # 65 535 entries announced in IFD0
    assert taken(bytes(data)) is None
    far = bytearray(tiff("<"))
    far[4:8] = struct.pack("<I", 0xFFFFFFF0)
    assert taken(bytes(far)) is None
    loop = b"\xff\xd8" + b"\xff\xfe\x00\x02" * 100_000  # 100 000 empty comments
    recorder = Recorder(loop)
    assert exif.taken_at(recorder, len(loop)) is None
    assert recorder.read_bytes <= 16 + 4 * exif._MAX_STEPS


def test_a_reader_that_returns_less_than_asked() -> None:
    data = jpeg(tiff())
    assert exif.taken_at(lambda offset, size: data[offset : offset + size][:3], len(data)) is None
    # The 20 bytes of the date come back as 19: a cut read is never parsed.
    cut = jpeg(tiff(tags={0x9003: b"2023:07:14 10:22:31\0"}))

    def short_date(offset: int, size: int) -> bytes:
        return cut[offset : offset + size - (size == len(DATE))]

    assert exif.taken_at(short_date, len(cut)) is None


def test_the_budget_is_a_hard_limit() -> None:
    """The fixed bounds keep a real file far below it; it is the last guard."""
    data = b"\0" * (exif.MAX_READ + 10)
    budget = exif._Budget(Recorder(data), len(data))
    budget.get(0, exif.MAX_READ - 4)
    with pytest.raises(exif._Damaged):
        budget.get(0, 5)


def test_nothing_after_the_image_data_of_a_jpeg_is_read() -> None:
    """Past the start of scan, bytes are image data that can look like markers."""
    whole = jpeg(tiff())
    start = whole.index(b"\xff\xe1")
    moved = whole[:start] + b"\xff\xda\0\x08" + b"\0" * 6 + whole[start:]
    assert taken(moved) is None


def test_the_announced_size_bounds_every_read() -> None:
    data = jpeg(tiff())
    # The file shrank since it was measured: nothing beyond the size is asked for.
    asked: list[tuple[int, int]] = []

    def read(offset: int, size: int) -> bytes:
        asked.append((offset, size))
        return data[offset : offset + size]

    assert exif.taken_at(read, 40) is None
    assert all(offset + size <= 40 for offset, size in asked)


def test_truncated_files_never_raise() -> None:
    for build in (jpeg, png, webp, heif):
        whole = build(tiff())
        for cut in range(len(whole)):
            taken(whole[:cut])


def test_random_damage_never_raises() -> None:
    chance = random.Random(71)  # noqa: S311 - reproducible damage, not secrecy
    for build in (lambda p: p, jpeg, png, webp, heif):
        whole = bytearray(build(tiff(chance.choice("<>"))))
        for _ in range(400):
            damaged = bytearray(whole)
            for _ in range(chance.randint(1, 8)):
                damaged[chance.randrange(len(damaged))] = chance.randrange(256)
            found = taken(bytes(damaged))
            assert found is None or isinstance(found, datetime)


def test_the_extensions_read() -> None:
    assert {"jpg", "jpeg", "heic", "avif", "dng", "nef", "cr2", "png", "webp"} <= exif.EXTENSIONS
    assert "gif" not in exif.EXTENSIONS and "svg" not in exif.EXTENSIONS
