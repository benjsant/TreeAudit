"""Read-only listing of the archive directory, for the "Mes archives" page (D-034)."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from treeaudit.archive.output import DIR_FLAGS, Compression
from treeaudit.archive.verify import printable

MAX_LISTED = 500
PARTIAL_SUFFIX = ".partial"
_SUFFIXES = tuple(compression.suffix for compression in Compression)


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    name: str
    """Printable name (D-014)."""
    size: int
    mtime_ns: int

    @property
    def incomplete(self) -> bool:
        return "-INCOMPLET." in self.name


@dataclass(frozen=True, slots=True)
class ArchiveListing:
    files: tuple[ArchiveFile, ...]
    total: int
    unfinished: int
    """Hidden temporary files left by an interrupted archive (power cut...)."""


def list_archives(archive_dir: str, *, limit: int = MAX_LISTED) -> ArchiveListing:
    """Archives found in the archive directory, newest first. Never follows a link."""
    fd = os.open(archive_dir, DIR_FLAGS)
    files: list[ArchiveFile] = []
    unfinished = 0
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    unfinished += name.endswith(PARTIAL_SUFFIX)
                    continue
                if not name.endswith(_SUFFIXES):
                    continue
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    files.append(
                        ArchiveFile(printable(os.fsencode(name)), st.st_size, st.st_mtime_ns)
                    )
    finally:
        os.close(fd)
    files.sort(key=lambda f: f.mtime_ns, reverse=True)
    return ArchiveListing(tuple(files[:limit]), len(files), unfinished)
