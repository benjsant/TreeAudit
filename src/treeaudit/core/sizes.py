"""Sizes: which size a file shows (D-047), and how a size is written.

A file shows its size, a folder the place it uses on the disk (D-042). A
sparse file (a disk image, a virtual machine, a database preallocated) can be
far bigger than the place it really uses: 1 GiB that takes 0 bytes. Shown at
its size, it would say "1 Gio" where nothing can be freed. Such a file shows
the place it uses instead, and is marked as sparse.
"""

from __future__ import annotations

SPARSE_MIN_GAP = 1024 * 1024
"""Below this, the gap is block rounding or a small hole: not worth a mention."""


def is_sparse(size: int, allocated: int) -> bool:
    """Far more apparent size than place used (a file compressed by the
    filesystem looks the same, and is shown the same way)."""
    return size - allocated >= SPARSE_MIN_GAP and allocated * 2 < size


def shown_file_size(size: int, allocated: int) -> int:
    """The size a file shows: its place on the disk when sparse, else its size."""
    return allocated if is_sparse(size, allocated) else size


def decimal(value: float) -> str:
    """One decimal, with the French decimal comma: 2.25 -> "2,2"."""
    return f"{value:.1f}".replace(".", ",")


def human_size(size: int | None) -> str:
    """A size as people read it, in binary units with French symbols and
    decimal comma: "512 o", "1,5 Kio", "3,2 Gio". None (unknown) is said in words."""
    if size is None:
        return "taille inconnue"
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if value < 1024 or unit == "Tio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{decimal(value)} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
