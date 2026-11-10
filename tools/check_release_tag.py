"""Check that a release tag names the version written in the code (D-062).

Run by the release workflow before anything is built:

    python3 -I tools/check_release_tag.py v1.0.0-rc.1

Accepted tags: vX.Y.Z and vX.Y.Z-rc.N only, so that the tag, which ends up in
a shell command and in the release title, is never anything else. Standard
library only: the GitHub runner has no project dependency installed.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?")
_INIT_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def version_of_tag(tag: str) -> str:
    """v1.0.0-rc.1 -> 1.0.0rc1, v1.0.0 -> 1.0.0; ValueError for anything else."""
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"tag {tag!r}: expected vX.Y.Z or vX.Y.Z-rc.N")
    major, minor, patch, candidate = match.groups()
    version = f"{major}.{minor}.{patch}"
    return f"{version}rc{candidate}" if candidate else version


def written_versions(repo: Path = REPO) -> tuple[str, str]:
    """The version of pyproject.toml and the one of treeaudit/__init__.py."""
    with (repo / "pyproject.toml").open("rb") as handle:
        project = str(tomllib.load(handle)["project"]["version"])
    init = (repo / "src" / "treeaudit" / "__init__.py").read_text(encoding="utf-8")
    found = _INIT_VERSION.search(init)
    if found is None:
        raise ValueError("no __version__ in src/treeaudit/__init__.py")
    return project, found.group(1)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_release_tag.py vX.Y.Z[-rc.N]", file=sys.stderr)
        return 2
    try:
        expected = version_of_tag(argv[0])
        project, init = written_versions()
    except (ValueError, OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if project != expected or init != expected:
        print(
            f"refused: tag {argv[0]} is version {expected}, but pyproject.toml says "
            f"{project} and treeaudit/__init__.py says {init}",
            file=sys.stderr,
        )
        return 1
    print(f"tag {argv[0]} matches version {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
