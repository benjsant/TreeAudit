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
from collections.abc import Iterable, Mapping
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
CATEGORIES = (
    PHOTOS,
    VIDEOS,
    MUSIC,
    DOCUMENTS,
    ARCHIVES,
    INSTALLERS,
    CODE,
    FONTS,
    PROJECTS,
    SOFTWARE,
    OTHER,
)
MAX_FOLDER_NAME = 60
MAX_ADJUSTMENTS = 100_000
EXCLUDED = "laissé en place à ta demande"

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


def clean_folder_name(text: str) -> str | None:
    """A folder name chosen by the user, or None if it is not acceptable.

    One plain name: no "/" (no sub-path, no escape upwards), not "." nor "..",
    not hidden nor starting with "-", no control character, short.
    """
    name = text.strip()
    if (
        not name
        or len(name) > MAX_FOLDER_NAME
        or name in (".", "..")
        or name.startswith((".", "-"))
        or "/" in name
        or "\\" in name
        or not is_displayable(name.encode())
    ):
        return None
    return name


class AdjustmentError(ValueError):
    """An adjustment of the plan that cannot be accepted."""


@dataclass(frozen=True, slots=True)
class Adjustments:
    """The user's changes to the plan: elements left in place, other categories,
    other folder names. Each one is checked again (adjustments_from_query)."""

    excluded: frozenset[int] = frozenset()
    categories: Mapping[int, str] = field(default_factory=dict)
    names: Mapping[str, str] = field(default_factory=dict)
    destination: str = DESTINATION

    def folder(self, category: str) -> str:
        return self.names.get(category, category)

    @property
    def any(self) -> bool:
        return bool(
            self.excluded or self.categories or self.names or self.destination != DESTINATION
        )


def adjustments_from_query(params: Iterable[tuple[str, str]]) -> Adjustments:
    """Read the adjustments from a page's query string, refusing anything odd.

    ``exclure=<id>``, ``categorie_<id>=<category>``, ``nom_<category>=<name>``,
    ``destination=<name>``. Empty values mean "unchanged"; other keys are ignored.
    """
    excluded: set[int] = set()
    categories: dict[int, str] = {}
    names: dict[str, str] = {}
    destination = DESTINATION
    for count, (key, value) in enumerate(params, start=1):
        if count > MAX_ADJUSTMENTS:
            raise AdjustmentError("trop d'ajustements")
        if key == "exclure":
            if not value.isdigit():
                raise AdjustmentError("élément inconnu")
            excluded.add(int(value))
        elif key.startswith("categorie_") and value:
            entry = key.removeprefix("categorie_")
            if not entry.isdigit() or value not in CATEGORIES:
                raise AdjustmentError(f"catégorie inconnue : {value}")
            categories[int(entry)] = value
        elif key.startswith("nom_") and value.strip():
            category = key.removeprefix("nom_")
            name = clean_folder_name(value)
            if category not in CATEGORIES or name is None:
                raise AdjustmentError(f"nom de dossier refusé : {value}")
            if name != category:
                names[category] = name
        elif key == "destination" and value.strip():
            chosen = clean_folder_name(value)
            if chosen is None:
                raise AdjustmentError(f"nom de dossier refusé : {value}")
            destination = chosen
    return Adjustments(frozenset(excluded), categories, names, destination)


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
    entry_id: int | None = None
    """Set when the user asked for it: the page can offer to move it again."""


@dataclass
class Plan:
    start: str
    """The chosen folder, relative to the analysed root ("" for the root)."""
    adjustments: Adjustments = field(default_factory=Adjustments)
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
    def destination(self) -> str:
        return self.adjustments.destination

    @property
    def excluded(self) -> list[Kept]:
        return [kept for kept in self.kept if kept.entry_id is not None]

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
    def __init__(self, start: _Node, adjustments: Adjustments) -> None:
        self.start = start
        self.adjust = adjustments
        self.plan = Plan(start=start.rel_display, adjustments=adjustments)
        self.taken: set[str] = set()

    def _relative(self, node: _Node) -> str:
        return node.rel_display[len(self.start.rel_display) :].lstrip("/")

    def _keep(self, node: _Node, reason: str) -> None:
        self.plan.kept.append(Kept(self._relative(node), reason))

    def _target(self, category: str, year: int | None, name: str) -> tuple[str, bool]:
        folder = f"{self.adjust.destination}/{self.adjust.folder(category)}"
        folder += f"/{year}" if year else ""
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

    def _move(self, node: _Node, category: str, reason: str) -> None:
        chosen = self.adjust.categories.get(node.entry_id)
        if chosen is not None and chosen != category:
            category, reason = chosen, "catégorie choisie par toi"
        year: int | None = None
        if category in BY_YEAR:
            year = (
                _year(node.mtime_ns) if node.kind != "dir" else node.years.most_common(1)[0][0]
            ) or None
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
            if child.kind == "dir" and child.name_display == self.adjust.destination:
                self._keep(child, "dossier de destination")
                continue
            self._visit(child)
        return self.plan

    def _visit(self, node: _Node) -> None:
        if node.entry_id in self.adjust.excluded:
            self.plan.kept.append(Kept(self._relative(node), EXCLUDED, node.entry_id))
            return
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
            self._move(node, category_of(node.extension), "fichier rangé par type")
            return
        block = _block_kind(node)
        if block is not None:
            reason = "projet, déplacé en entier" if block == PROJECTS else "logiciel ou jeu"
            self._move(node, block, reason)
            return
        if not node.files:
            self._keep(node, "dossier vide")
            return
        if len(node.categories) == 1 and not node.unmovable and not node.has_block:
            (category,) = node.categories
            self._move(node, category, "dossier homogène, gardé tel quel")
            return
        for child in node.children:
            self._visit(child)


def make_plan(
    conn: sqlite3.Connection,
    scan_id: int,
    start_id: int,
    adjustments: Adjustments | None = None,
) -> Plan:
    """The plan for the folder ``start_id`` of a finished analysis."""
    start = _load(conn, scan_id, start_id)
    if start.kind != "dir":
        raise KeyError(start_id)
    _summarize(start)
    return _Planner(start, adjustments or Adjustments()).run()
