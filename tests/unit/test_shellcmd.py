"""Names that can be written in a proposed command."""

from __future__ import annotations

import pytest

from treeaudit.shellcmd import is_displayable


@pytest.mark.parametrize(
    "name",
    ["photo\u202egpj.exe", "a\u2066b", "x\u200fy", "c1\x85control", "csi\x9b31m"],
)
def test_marks_that_change_the_writing_direction_get_no_command(name: str) -> None:
    # "photo\u202Egpj.exe" reads "photoexe.jpg" (security review V1).
    assert not is_displayable(f"dossier/{name}".encode())
    assert is_displayable("dossier/Été à l'île — n°2.jpg".encode())
