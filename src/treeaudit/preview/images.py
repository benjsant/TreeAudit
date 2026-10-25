"""Image preview (D-046): the original bytes of an image, read safely.

Nothing is decoded here: the browser, sandboxed, shows the image. The file
is read like the duplicates do (duplicates/reader.py): every folder opened
with O_NOFOLLOW from the root, the file must be the one of the index (device,
inode, size, date) before and after it is read. Its type comes from its
first bytes, never from its name; SVG, which can carry scripts, is never
served. Files inside private folders are never read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from treeaudit.db.scans import extension_of
from treeaudit.duplicates.reader import (
    Expected,
    FileChangedError,
    RootChangedError,
    TreeReader,
    check_unchanged,
)

MAX_PREVIEW_SIZE = 20 * 1024 * 1024
"""Bigger images are not previewed: the whole file is held in memory."""
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "jpe", "png", "gif", "webp", "bmp", "avif"})
_CHUNK = 1024 * 1024


class PreviewError(Exception):
    """No preview for this file; the message says why, in French."""


@dataclass(frozen=True, slots=True)
class Image:
    content: bytes
    media_type: str


def sniff(head: bytes) -> str | None:
    """Media type of an image from its first bytes, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM") and len(head) >= 26:
        return "image/bmp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def previewable(kind: str, extension: str | None, size: int | None) -> bool:
    """Worth offering a preview, from the index alone (the content decides later)."""
    return (
        kind == "file"
        and extension in IMAGE_EXTENSIONS
        and size is not None
        and 0 < size <= MAX_PREVIEW_SIZE
    )


def looks_like_image(kind: str, rel_path: bytes, size: int | None) -> bool:
    """previewable() from a path, for pages that do not have the extension at hand."""
    return previewable(kind, extension_of(rel_path.rpartition(b"/")[2]), size)


def _inside(rel_path: bytes, folders: frozenset[bytes]) -> bool:
    return any(rel_path == f or rel_path.startswith(f + b"/") for f in folders)


def read_image(
    root_path: str,
    expected_root: tuple[int, int],
    rel_path: bytes,
    expected: Expected,
    *,
    private_paths: frozenset[bytes] = frozenset(),
) -> Image:
    """The image at ``rel_path`` of the root, exactly as the analysis saw it."""
    if _inside(rel_path, private_paths):
        raise PreviewError("dossier privé : jamais lu")
    if not 0 < expected.size <= MAX_PREVIEW_SIZE:
        raise PreviewError("image trop grande pour un aperçu")
    try:
        with TreeReader(root_path, expected_root) as reader:
            fd = reader.open_file(rel_path, expected)
            try:
                parts: list[bytes] = []
                remaining = expected.size
                while remaining > 0:
                    chunk = os.read(fd, min(_CHUNK, remaining))
                    if not chunk:
                        break
                    parts.append(chunk)
                    remaining -= len(chunk)
                check_unchanged(fd, expected)
            finally:
                os.close(fd)
    except (RootChangedError, FileChangedError) as exc:
        raise PreviewError("fichier modifié depuis l'analyse : relance une analyse") from exc
    except OSError as exc:
        raise PreviewError("fichier illisible ou déplacé depuis l'analyse") from exc
    content = b"".join(parts)
    if remaining or len(content) != expected.size:
        raise PreviewError("fichier modifié pendant la lecture")
    media_type = sniff(content[:32])
    if media_type is None:
        raise PreviewError("ce fichier n'est pas une image reconnue")
    return Image(content, media_type)
