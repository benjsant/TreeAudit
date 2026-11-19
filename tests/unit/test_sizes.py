"""Sparse files show the place they use (D-047); sizes are written in French."""

from __future__ import annotations

import pytest

from treeaudit.core.sizes import decimal, human_size, is_sparse, shown_file_size

MIB = 1024 * 1024


@pytest.mark.parametrize(
    ("size", "allocated", "sparse"),
    [
        (1024 * MIB, 0, True),  # an empty 1 GiB image
        (10 * MIB, 2 * MIB, True),
        (32, 4096, False),  # a small file: block rounding, shown at its size
        (10 * MIB, 10 * MIB, False),
        (10 * MIB, 6 * MIB, False),  # more than half used
        (MIB - 1, 0, False),  # gap too small to mention
    ],
)
def test_sparse(size: int, allocated: int, sparse: bool) -> None:
    assert is_sparse(size, allocated) is sparse
    assert shown_file_size(size, allocated) == (allocated if sparse else size)


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (None, "taille inconnue"),
        (0, "0 o"),
        (1023, "1023 o"),
        (1536, "1,5 Kio"),
        (int(25.1 * MIB), "25,1 Mio"),
        (3 * 1024 * MIB + 100 * MIB, "3,1 Gio"),
    ],
)
def test_sizes_use_the_decimal_comma(size: int | None, text: str) -> None:
    assert human_size(size) == text


def test_decimal() -> None:
    assert (decimal(0.06), decimal(12.0)) == ("0,1", "12,0")
