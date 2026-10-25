"""Sparse files show the place they use (D-047)."""

from __future__ import annotations

import pytest

from treeaudit.core.sizes import is_sparse, shown_file_size

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
