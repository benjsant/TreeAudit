"""Duplicate groups built from a finished analysis, with the copy to keep.

A group is a set of distinct inodes with the same size and SHA-256. Every path
of these inodes is a copy (hard links included, shown together). Some copies are
protected and never proposed for removal: marker files whose presence matters
(``__init__.py``, ``.gitkeep``...), anything inside a git repository and anything
inside installed software or dependencies (games, Wine prefixes, node_modules...). When no
copy is protected, one copy to keep is suggested with its reasons; the user has
the final say.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from dataclasses import dataclass, field

from treeaudit.db.connection import from_sqlite_int

PROTECTED_NAMES = frozenset(
    {
        b"__init__.py",
        b"__init__.pyi",
        b"py.typed",
        b".gitkeep",
        b".keep",
        b".gitignore",
        b".gitattributes",
        b".gitmodules",
        b".npmignore",
        b".dockerignore",
        b".nojekyll",
        b".placeholder",
        b"CACHEDIR.TAG",
    }
)

# Folders of installed software or dependencies: identical files there are on
# purpose (one copy per game, per environment...) and removing one breaks it.
# Rebuildable dependency folders are handled as a whole by `scan parasites`.
SOFTWARE_DIRS = frozenset(
    {
        b"steamapps",
        b"compatdata",
        b"Program Files",
        b"Program Files (x86)",
        b"ProgramData",
        b"drive_c",
        b".wine",
        b"site-packages",
        b"dist-packages",
        b"node_modules",
        b"bower_components",
        b".venv",
        b"venv",
        b"vendor",
        b".flatpak",
        b"flatpak",
        b"snap",
    }
)

_SUSPICIOUS = (
    (re.compile(r"(?i)\bcop(y|ie)\b"), "« copie » dans le chemin"),
    # "photo (1).jpg", "notes (2)": a space, a number in brackets, at the end of the name.
    (re.compile(r" \(\d+\)(\.[^./]*)?$"), "numéro de copie « (1) » dans le nom"),
    (
        re.compile(r"(?i)\b(backups?|sauvegardes?|bak|old|ancien|archives?)\b"),
        "dossier de sauvegarde ou d'archives",
    ),
    (re.compile(r"(?i)\b(tmp|temp|trash|corbeille)\b"), "dossier temporaire ou corbeille"),
    (re.compile(r"(?i)\b(downloads|t[ée]l[ée]chargements)\b"), "dossier de téléchargements"),
)


@dataclass(frozen=True, slots=True)
class Copy:
    entry_id: int
    rel_path: bytes
    rel_path_display: str
    mtime_ns: int
    inode: tuple[int, int]
    protection: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    size: int
    digest: bytes | None
    """SHA-256 of the content; None for the group of empty files."""
    copies: tuple[Copy, ...]
    keep: tuple[Copy, ...]
    keep_reasons: tuple[str, ...]
    number: int = 0
    zone: str | None = None
    """For empty files: the area (first two folder levels) they were found in."""

    @property
    def is_empty_files(self) -> bool:
        return self.digest is None

    @property
    def removable(self) -> tuple[Copy, ...]:
        kept = {c.entry_id for c in self.keep}
        return tuple(c for c in self.copies if c.entry_id not in kept)

    @property
    def recoverable(self) -> int:
        """Bytes freed by removing every removable copy (hard links counted once)."""
        kept_inodes = {c.inode for c in self.keep}
        freed = {c.inode for c in self.removable} - kept_inodes
        return len(freed) * self.size

    @property
    def inodes(self) -> int:
        return len({c.inode for c in self.copies})


@dataclass(frozen=True, slots=True)
class GroupReport:
    groups: list[DuplicateGroup]
    hardlinked_inodes: int
    protected_copies: int
    micro: bool
    options: dict[str, object] = field(default_factory=dict)


def suspicious_reasons(path: str) -> list[str]:
    return [label for pattern, label in _SUSPICIOUS if pattern.search(path)]


def choose_keep(copies: list[Copy]) -> tuple[tuple[Copy, ...], tuple[str, ...]]:
    """Copies to keep and why. Protected copies are always kept."""
    protected = tuple(c for c in copies if c.protection)
    free = [c for c in copies if not c.protection]
    protected_reasons = tuple(sorted({f"protégée : {c.protection}" for c in protected}))
    if not free:
        return protected, protected_reasons

    def rank(c: Copy) -> tuple[int, int, int, str]:
        return (
            len(suspicious_reasons(c.rel_path_display)),
            c.mtime_ns,
            len(c.rel_path_display),
            c.rel_path_display,
        )

    # Protected copies belong to software or projects: they are never the user's
    # only copy. Keep one free copy too, so removing the others loses nothing.
    best = min(free, key=rank)
    others = [c for c in free if c is not best]
    reasons: list[str] = []
    if any(suspicious_reasons(o.rel_path_display) for o in others) and not suspicious_reasons(
        best.rel_path_display
    ):
        reasons.append("seul chemin sans indice de copie (copie, (1), backup, old, Downloads…)")
    if all(best.mtime_ns < o.mtime_ns for o in others):
        reasons.append("fichier le plus ancien (probablement l'original)")
    if not reasons:
        reasons.append("chemin le plus court")
    if protected:
        reasons = [*protected_reasons, "et une copie hors logiciel/projet : " + ", ".join(reasons)]
    return (*protected, best), tuple(reasons)


def _repo_dirs(conn: sqlite3.Connection, scan_id: int) -> set[bytes]:
    """Directories containing a .git entry (file or directory)."""
    rows = conn.execute(
        "SELECT rel_path FROM entry WHERE scan_id = ? AND name = ?", (scan_id, b".git")
    )
    return {row[0].rpartition(b"/")[0] for row in rows}


def _protection(rel_path: bytes, repo_dirs: set[bytes]) -> str | None:
    parent, _, name = rel_path.rpartition(b"/")
    if name in PROTECTED_NAMES:
        return f"fichier-marqueur {name.decode('utf-8', 'backslashreplace')}"
    parts = parent.split(b"/") if parent else []
    for depth in range(len(parts), -1, -1):
        if b"/".join(parts[:depth]) in repo_dirs:
            return "dans un dépôt git"
    for part in parts:
        if part in SOFTWARE_DIRS or part.endswith(b".app"):
            label = part.decode("utf-8", "backslashreplace")
            return f"logiciel ou dépendances installés ({label})"
    return None


def _copy(row: sqlite3.Row, protection: str | None) -> Copy:
    return Copy(
        entry_id=row["entry_id"],
        rel_path=row["rel_path"],
        rel_path_display=row["rel_path_display"],
        mtime_ns=row["mtime_ns"],
        inode=(from_sqlite_int(row["dev"]), from_sqlite_int(row["ino"])),
        protection=protection,
    )


EMPTY_ZONE_DEPTH = 2


def empty_zone(rel_path: bytes) -> bytes:
    """Area of an empty file: the first EMPTY_ZONE_DEPTH folders of its parent."""
    parent = rel_path.rpartition(b"/")[0]
    return b"/".join(parent.split(b"/")[:EMPTY_ZONE_DEPTH]) if parent else b""


def _make_group(size: int, digest: bytes | None, copies: list[Copy]) -> DuplicateGroup:
    copies.sort(key=lambda c: c.rel_path)
    keep: tuple[Copy, ...]
    reasons: tuple[str, ...]
    if digest is None:  # empty files: nothing to keep, except protected markers
        keep = tuple(c for c in copies if c.protection)
        reasons = ("fichiers vides : aucun contenu à conserver",)
    else:
        keep, reasons = choose_keep(copies)
    return DuplicateGroup(size, digest, tuple(copies), keep, reasons)


def load_groups(conn: sqlite3.Connection, run_id: int) -> GroupReport:
    run = conn.execute("SELECT scan_id, options FROM dup_run WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(run_id)
    scan_id = run["scan_id"]
    options = json.loads(run["options"])
    micro = bool(options.get("micro"))
    repo_dirs = _repo_dirs(conn, scan_id)

    groups: list[DuplicateGroup] = []
    digests = conn.execute(
        "SELECT size, full FROM file_hash WHERE run_id = ? AND full IS NOT NULL "
        "GROUP BY size, full HAVING COUNT(*) >= 2",
        (run_id,),
    ).fetchall()
    for digest_row in digests:
        rows = conn.execute(
            "SELECT e.entry_id, e.rel_path, e.rel_path_display, e.mtime_ns, e.dev, e.ino "
            "FROM file_hash h "
            "JOIN entry r ON r.scan_id = ? AND r.entry_id = h.entry_id "
            "JOIN entry e ON e.scan_id = r.scan_id AND e.dev = r.dev AND e.ino = r.ino "
            "AND e.kind = 'file' "
            "WHERE h.run_id = ? AND h.size = ? AND h.full = ?",
            (scan_id, run_id, digest_row["size"], digest_row["full"]),
        )
        copies = [_copy(row, _protection(row["rel_path"], repo_dirs)) for row in rows]
        groups.append(_make_group(digest_row["size"], digest_row["full"], copies))

    protected_empty = 0
    if micro:
        empty = conn.execute(
            "SELECT entry_id, rel_path, rel_path_display, mtime_ns, dev, ino FROM entry "
            "WHERE scan_id = ? AND kind = 'file' AND size = 0",
            (scan_id,),
        )
        # Empty files free no space and programs may rely on them (markers, locks,
        # games): group them by area so that the user decides area by area.
        # Protected ones are only counted: there can be hundreds of thousands.
        zones: dict[bytes, list[Copy]] = {}
        for row in empty:
            protection = _protection(row["rel_path"], repo_dirs)
            if protection:
                protected_empty += 1
                continue
            zones.setdefault(empty_zone(row["rel_path"]), []).append(_copy(row, None))
        for zone, copies in sorted(zones.items()):
            if len(copies) < 2:
                continue
            group = _make_group(0, None, copies)
            label = zone.decode("utf-8", "backslashreplace") or "."
            groups.append(dataclasses.replace(group, zone=label))

    protected = protected_empty + sum(1 for g in groups for c in g.copies if c.protection)
    groups = [g for g in groups if g.removable]
    if micro:
        groups.sort(key=lambda g: (-len(g.removable), -g.recoverable))
    else:
        groups.sort(key=lambda g: (-g.recoverable, -len(g.removable)))
    numbered = [dataclasses.replace(g, number=index) for index, g in enumerate(groups, start=1)]
    hardlinked = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM entry WHERE scan_id = ? AND kind = 'file' "
        "GROUP BY dev, ino HAVING COUNT(*) > 1)",
        (scan_id,),
    ).fetchone()[0]
    return GroupReport(numbered, hardlinked, protected, micro, options)
