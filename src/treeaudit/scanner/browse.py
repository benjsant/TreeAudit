"""Live listing of the sub-folders of a root, for the folder picker (D-021).

Only directory names are returned; file contents are never read. The requested
folder is a path relative to the root, validated by ``parse_subpath`` and then
opened component by component from a descriptor of the root with O_NOFOLLOW,
so a symlink can never lead outside the root. Symlinks to directories are not
listed as folders (they are never followed by the scanner either).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from treeaudit.scanner.walker import _open_dir, parse_subpath
from treeaudit.shellcmd import is_displayable

DEFAULT_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class Listing:
    path: str
    """The listed folder, relative to the root ('' for the root itself)."""
    folders: tuple[str, ...]
    """Names of the sub-folders, sorted case-insensitively."""
    truncated: bool
    hidden: int
    """Sub-folders not shown because their name cannot be displayed safely."""


def list_folders(root_path: str, subpath: str, *, limit: int = DEFAULT_LIMIT) -> Listing:
    """List the sub-folders of ``subpath`` inside the root.

    Raises SubpathError for an unsafe relative path and OSError when a component
    is missing, not a directory, a symlink or not readable.
    """
    parts = parse_subpath(subpath)
    fd = _open_dir(root_path, None)
    try:
        for part in parts:
            child = _open_dir(part, fd)
            os.close(fd)
            fd = child
        names: list[str] = []
        hidden = 0
        with os.scandir(fd) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # vanished or not accessible: simply not proposed
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if not is_displayable(os.fsencode(entry.name)):
                    hidden += 1
                    continue
                names.append(entry.name)
    finally:
        os.close(fd)
    names.sort(key=lambda name: (name.casefold(), name))
    return Listing(
        path="/".join(parts),
        folders=tuple(names[:limit]),
        truncated=len(names) > limit,
        hidden=hidden,
    )
