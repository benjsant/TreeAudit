"""The release tag check (D-062): only plain version tags, matching the code."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from treeaudit import __version__

REPO = Path(__file__).resolve().parents[2]


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_release_tag", REPO / "tools" / "check_release_tag.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_tool()


@pytest.mark.parametrize(
    ("tag", "version"),
    [("v1.0.0", "1.0.0"), ("v1.0.0-rc.1", "1.0.0rc1"), ("v2.10.3-rc.12", "2.10.3rc12")],
)
def test_plain_version_tags_are_read(tag: str, version: str) -> None:
    assert tool.version_of_tag(tag) == version


@pytest.mark.parametrize(
    "tag",
    [
        "1.0.0",
        "v1.0",
        "v01.0.0",
        "v1.0.0-beta.1",
        "v1.0.0-rc.0",
        "v1.0.0rc1",
        "v1.0.0-rc.1;id",
        "v1.0.0$(touch x)",
        "v1.0.0\n",
    ],
)
def test_anything_else_is_refused(tag: str) -> None:
    with pytest.raises(ValueError, match="expected"):
        tool.version_of_tag(tag)


def test_the_two_written_versions_agree() -> None:
    assert tool.written_versions() == (__version__, __version__)


def test_a_tag_for_another_version_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert tool.main(["v9.9.9"]) == 1
    assert "refused" in capsys.readouterr().err
    tag = "v" + __version__.replace("rc", "-rc.")
    assert tool.main([tag]) == 0
