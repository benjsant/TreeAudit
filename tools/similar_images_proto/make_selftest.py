"""Write the prototype's self-test image: a small lossless PNG with known pixels.

A browser that adds noise to what a page reads back (anti-fingerprinting) changes
them; the page counts the wrong pixels before hashing anything.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import zlib

SIDE = 8


def pixels() -> bytes:
    """RGB values of every pixel, row by row: a pattern with no flat area."""
    values = []
    for y in range(SIDE):
        for x in range(SIDE):
            values += [(x * 31 + y * 7) % 256, (x * 13 + y * 29) % 256, (x * y * 5 + 40) % 256]
    return bytes(values)


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png(rgb: bytes) -> bytes:
    """An 8-bit RGB PNG of those pixels, no filter, no colour profile."""
    rows = b"".join(b"\x00" + rgb[y * SIDE * 3 : (y + 1) * SIDE * 3] for y in range(SIDE))
    header = struct.pack(">IIBBBBB", SIDE, SIDE, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows, 9))
        + _chunk(b"IEND", b"")
    )


def main(target: str) -> None:
    """Write {"png": base64, "rgb": [...]} to ``target``."""
    rgb = pixels()
    with open(target, "w", encoding="utf-8") as handle:
        json.dump({"png": base64.b64encode(png(rgb)).decode(), "rgb": list(rgb)}, handle)


if __name__ == "__main__":
    main(sys.argv[1])
