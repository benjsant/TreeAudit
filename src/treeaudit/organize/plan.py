"""Tidying plan: where each element of a folder would go (D-041, step A).

Computed from the index only: nothing on the disk is read or changed. The
plan is later turned into a script the user reviews and runs (script.py).

From the most careful to the finest:
1. blocks: projects, git repositories, software and games move whole;
2. homogeneous folders (all their files of one category, e.g. an album)
   move whole, under their own name: the user's own sorting is kept;
3. loose files of mixed folders move one by one, by type (and by year for
   photos and videos).
Hidden entries, links, special files, names a command cannot carry
unambiguously, and the destination folder itself stay where they are.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from treeaudit.duplicates.groups import SOFTWARE_DIRS
from treeaudit.shellcmd import is_displayable

DESTINATION = "Rangé"
MAX_ENTRIES = 300_000
"""Beyond this, the plan would take too long and the script be too big: choose
a sub-folder instead."""

PHOTOS, VIDEOS, MUSIC, DOCUMENTS = "Photos", "Vidéos", "Musique", "Documents"
ARCHIVES, INSTALLERS, CODE, FONTS, OTHER = "Archives", "Installateurs", "Code", "Polices", "Autres"
PROJECTS, SOFTWARE = "Projets", "Logiciels"
BY_YEAR = frozenset({PHOTOS, VIDEOS})

_EXTENSIONS: dict[str, frozenset[str]] = {
    PHOTOS: frozenset(
        [
            "jpg",
            "jpeg",
            "png",
            "gif",
            "bmp",
            "tif",
            "tiff",
            "webp",
            "heic",
            "heif",
            "avif",
            "raw",
            "cr2",
            "cr3",
            "nef",
            "arw",
            "dng",
            "orf",
            "rw2",
            "raf",
            "psd",
            "xcf",
            "svg",
        ]
    ),
    VIDEOS: frozenset(
        [
            "mp4",
            "m4v",
            "mkv",
            "avi",
            "mov",
            "wmv",
            "flv",
            "webm",
            "mpg",
            "mpeg",
            "3gp",
            "mts",
            "m2ts",
            "vob",
            "ts",
        ]
    ),
    MUSIC: frozenset(
        [
            "mp3",
            "flac",
            "ogg",
            "oga",
            "opus",
            "m4a",
            "aac",
            "wav",
            "wma",
            "aiff",
            "aif",
            "ape",
            "alac",
            "mid",
            "midi",
        ]
    ),
    DOCUMENTS: frozenset(
        [
            "pdf",
            "doc",
            "docx",
            "odt",
            "rtf",
            "txt",
            "md",
            "tex",
            "xls",
            "xlsx",
            "ods",
            "csv",
            "ppt",
            "pptx",
            "odp",
            "epub",
            "mobi",
            "djvu",
            "pages",
            "numbers",
            "key",
            "vcf",
            "ics",
            "eml",
            "msg",
        ]
    ),
    ARCHIVES: frozenset(
        ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "img", "cab"]
    ),
    INSTALLERS: frozenset(
        ["exe", "msi", "deb", "rpm", "appimage", "dmg", "pkg", "apk", "flatpakref", "jar"]
    ),
    CODE: frozenset(
        [
            "py",
            "js",
            "ts",
            "c",
            "h",
            "cpp",
            "hpp",
            "cs",
            "java",
            "kt",
            "rs",
            "go",
            "rb",
            "php",
            "sh",
            "bat",
            "ps1",
            "html",
            "htm",
            "css",
            "scss",
            "json",
            "xml",
            "yml",
            "yaml",
            "toml",
            "sql",
            "ipynb",
        ]
    ),
    FONTS: frozenset(["ttf", "otf", "woff", "woff2"]),
}
_CATEGORY_OF = {ext: category for category, exts in _EXTENSIONS.items() for ext in exts}

# A folder holding one of these at its top is a project: it moves whole.
PROJECT_MARKERS = frozenset(
    {
        b".git",
        b"package.json",
        b"Cargo.toml",
        b"pyproject.toml",
        b"setup.py",
        b"requirements.txt",
        b"go.mod",
        b"pom.xml",
        b"build.gradle",
        b"composer.json",
        b"Gemfile",
        b"CMakeLists.txt",
        b"Makefile",
        b"meson.build",
        b".project",
    }
)
_PROJECT_SUFFIXES = (b".csproj", b".sln", b".vcxproj", b".xcodeproj", b".uproject")
# Folders that only make sense inside the program that holds them.
_INSTALL_DIRS = frozenset(
    {
        b"steamapps",
        b"compatdata",
        b"drive_c",
        b"Program Files",
        b"Program Files (x86)",
        b"ProgramData",
    }
)
# Desktop clutter that must not make an album "mixed".
NEUTRAL_FILES = frozenset({b"Thumbs.db", b".DS_Store", b"desktop.ini", b".directory"})


def category_of(extension: str | None) -> str:
    return _CATEGORY_OF.get(extension or "", OTHER)


class PlanTooLargeError(ValueError):
    """The chosen folder holds more than MAX_ENTRIES entries."""


@dataclass(frozen=True, slots=True)
class Move:
    entry_id: int
    source: str
    """Path relative to the chosen folder."""
    target: str
    """Path relative to the chosen folder, below DESTINATION."""
    kind: str  # "file" or "dir"
    category: str
    size: int
    files: int
    mtime_s: int
    reason: str
    renamed: bool = False


@dataclass(frozen=True, slots=True)
class Kept:
    path: str
    reason: str


@dataclass
class Plan:
    start: str
    """The chosen folder, relative to the analysed root ("" for the root)."""
    moves: list[Move] = field(default_factory=list)
    kept: list[Kept] = field(default_factory=list)

    def categories(self) -> dict[str, tuple[int, int, int]]:
        """Category -> (elements moved, files, bytes), in display order."""
        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for move in self.moves:
            total = totals[move.category]
            total[0] += 1
            total[1] += move.files
            total[2] += move.size
        return {name: (t[0], t[1], t[2]) for name, t in sorted(totals.items())}

    @property
    def renamed(self) -> list[Move]:
        return [move for move in self.moves if move.renamed]


@dataclass
class _Node:
    entry_id: int
    parent_id: int | None
    name: bytes
    name_display: str
    rel_path: bytes
    rel_display: str
    kind: str
    extension: str | None
    size: int
    mtime_ns: int
    children: list[_Node] = field(default_factory=list)
    # Filled bottom-up for folders:
    categories: Counter[str] = field(default_factory=Counter)
    years: Counter[int] = field(default_factory=Counter)
    files: int = 0
    total: int = 0
    unmovable: bool = False
    """Holds something that must stay put (hidden, link, special, bad name)."""
    has_block: bool = False
    """Holds a project or a program: never moved as a "homogeneous" folder."""


def _year(mtime_ns: int) -> int:
    try:
        return datetime.fromtimestamp(mtime_ns / 1e9, UTC).year
    except (OverflowError, OSError, ValueError):
        return 0


def _load(conn: sqlite3.Connection, scan_id: int, start_id: int) -> _Node:
    rows = conn.execute(LOAD_SQL, (start_id, scan_id, scan_id, MAX_ENTRIES + 1)).fetchall()
    if len(rows) > MAX_ENTRIES:
        raise PlanTooLargeError(len(rows))
    nodes = {
        row["entry_id"]: _Node(
            entry_id=row["entry_id"],
            parent_id=row["parent_id"],
            name=bytes(row["name"]),
            name_display=row["name_display"],
            rel_path=bytes(row["rel_path"]),
            rel_display=row["rel_path_display"],
            kind=row["kind"],
            extension=row["extension"],
            size=row["size"],
            mtime_ns=row["mtime_ns"],
        )
        for row in rows
    }
    if start_id not in nodes:
        raise KeyError(start_id)
    for node in nodes.values():
        if node.entry_id != start_id and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
    for node in nodes.values():
        node.children.sort(key=lambda n: n.rel_path)
    return nodes[start_id]


# The join order and the index are forced: left to itself, SQLite walked down
# with the inode index and read every entry of the analysis for each folder
# (152 970 entries: more than ten minutes instead of a fraction of a second).
LOAD_SQL = (
    "WITH RECURSIVE sub(id) AS ("
    "  SELECT ? UNION ALL"
    "  SELECT e.entry_id FROM sub CROSS JOIN entry e INDEXED BY entry_by_parent"
    "  ON e.scan_id = ? AND e.parent_id = sub.id"
    ") "
    "SELECT e.entry_id, e.parent_id, e.name, e.name_display, e.rel_path, "
    "e.rel_path_display, e.kind, e.extension, e.size, e.mtime_ns "
    "FROM sub CROSS JOIN entry e ON e.scan_id = ? AND e.entry_id = sub.id LIMIT ?"
)


def _hidden(node: _Node) -> bool:
    return node.name.startswith(b".")


def _summarize(node: _Node) -> None:
    """Bottom-up: categories, years and size of the files below each folder."""
    stack: list[tuple[_Node, bool]] = [(node, False)]
    while stack:
        current, done = stack.pop()
        if current.kind != "dir":
            continue
        if not done:
            stack.append((current, True))
            stack.extend((child, False) for child in current.children)
            continue
        for child in current.children:
            if child.kind == "dir":
                current.categories.update(child.categories)
                current.years.update(child.years)
                current.files += child.files
                current.total += child.total
                current.unmovable |= child.unmovable or _hidden(child)
                current.has_block |= child.has_block or _is_block(child)
            elif child.kind == "file":
                current.files += 1
                current.total += child.size
                if child.name in NEUTRAL_FILES:
                    continue
                if _hidden(child) or not is_displayable(child.rel_path):
                    current.unmovable = True
                    continue
                category = category_of(child.extension)
                current.categories[category] += 1
                if category in BY_YEAR:
                    current.years[_year(child.mtime_ns)] += 1
            else:  # symlink or special file: never moved alone
                current.unmovable = True


def _block_kind(node: _Node) -> str | None:
    """ "Projets", "Logiciels" or None."""
    names = {child.name for child in node.children}
    if names & PROJECT_MARKERS or any(n.endswith(_PROJECT_SUFFIXES) for n in names):
        return PROJECTS
    if node.name in SOFTWARE_DIRS or node.name.endswith(b".app"):
        return SOFTWARE
    # Its parent is the program: Steam/steamapps, a Wine prefix's drive_c, a
    # copied "C:" with Program Files... moving the inner folder alone breaks it.
    if any(child.kind == "dir" and child.name in _INSTALL_DIRS for child in node.children):
        return SOFTWARE
    extensions = {child.extension for child in node.children if child.kind == "file"}
    if "exe" in extensions and "dll" in extensions:  # an installed Windows program
        return SOFTWARE
    return None


def _is_block(node: _Node) -> bool:
    return node.kind == "dir" and _block_kind(node) is not None


class _Planner:
    def __init__(self, start: _Node) -> None:
        self.start = start
        self.plan = Plan(start=start.rel_display)
        self.taken: set[str] = set()

    def _relative(self, node: _Node) -> str:
        return node.rel_display[len(self.start.rel_display) :].lstrip("/")

    def _keep(self, node: _Node, reason: str) -> None:
        self.plan.kept.append(Kept(self._relative(node), reason))

    def _target(self, category: str, year: int | None, name: str) -> tuple[str, bool]:
        folder = f"{DESTINATION}/{category}" + (f"/{year}" if year else "")
        target = f"{folder}/{name}"
        number = 1
        stem, dot, suffix = name.rpartition(".")
        if not stem:  # no extension (or a dot only at the start)
            stem, dot, suffix = name, "", ""
        while target in self.taken:
            number += 1
            target = f"{folder}/{stem} ({number}){dot}{suffix}"
        self.taken.add(target)
        return target, number > 1

    def _move(self, node: _Node, category: str, year: int | None, reason: str) -> None:
        target, renamed = self._target(category, year, node.name_display)
        self.plan.moves.append(
            Move(
                entry_id=node.entry_id,
                source=self._relative(node),
                target=target,
                kind="dir" if node.kind == "dir" else "file",
                category=category,
                size=node.total if node.kind == "dir" else node.size,
                files=node.files if node.kind == "dir" else 1,
                mtime_s=node.mtime_ns // 1_000_000_000,
                reason=reason,
                renamed=renamed,
            )
        )

    def run(self) -> Plan:
        for child in self.start.children:
            if child.kind == "dir" and child.name.decode("utf-8", "replace") == DESTINATION:
                self._keep(child, "dossier de destination")
                continue
            self._visit(child)
        return self.plan

    def _visit(self, node: _Node) -> None:
        if not is_displayable(node.rel_path):
            self._keep(node, "nom impossible à écrire sans ambiguïté dans une commande")
            return
        if _hidden(node):
            self._keep(node, "élément caché (réglages, données d'application)")
            return
        if node.kind == "symlink":
            self._keep(node, "raccourci (lien symbolique)")
            return
        if node.kind == "other":
            self._keep(node, "fichier spécial")
            return
        if node.kind == "file":
            if node.name in NEUTRAL_FILES:
                return  # desktop clutter: left with its folder
            category = category_of(node.extension)
            year = _year(node.mtime_ns) if category in BY_YEAR else None
            self._move(node, category, year, "fichier rangé par type")
            return
        block = _block_kind(node)
        if block is not None:
            reason = "projet, déplacé en entier" if block == PROJECTS else "logiciel ou jeu"
            self._move(node, block, None, reason)
            return
        if not node.files:
            self._keep(node, "dossier vide")
            return
        if len(node.categories) == 1 and not node.unmovable and not node.has_block:
            (category,) = node.categories
            year = node.years.most_common(1)[0][0] if category in BY_YEAR else None
            self._move(node, category, year or None, "dossier homogène, gardé tel quel")
            return
        for child in node.children:
            self._visit(child)


def make_plan(conn: sqlite3.Connection, scan_id: int, start_id: int) -> Plan:
    """The plan for the folder ``start_id`` of a finished analysis."""
    start = _load(conn, scan_id, start_id)
    if start.kind != "dir":
        raise KeyError(start_id)
    _summarize(start)
    return _Planner(start).run()
