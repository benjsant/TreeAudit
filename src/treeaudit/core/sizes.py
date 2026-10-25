"""Which size a file shows (D-047).

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
