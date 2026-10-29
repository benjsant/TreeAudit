"""Projects and software found in an analysis (D-044), from the index only.

A project is a folder holding a marker file (package.json, Cargo.toml...) or a
git repository; software is a game library, a Wine prefix, a copied Windows,
a Windows program, a macOS application or an AppImage. Only the outermost
one counts: nothing is looked for inside a project or a program already
found, nor inside dependency or hidden folders. Names only say where to look;
nothing here decides what may be read or written.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from treeaudit.parasites.cached import parasite_findings
from treeaudit.parasites.detect import DEFAULT_MIN_CONFIDENCE, Finding

PROJECT, SOFTWARE = "projet", "logiciel"

# Marker file or folder name -> kind of project.
MARKERS: dict[bytes, str] = {
    b"package.json": "JavaScript / TypeScript",
    b"deno.json": "JavaScript / TypeScript",
    b"pyproject.toml": "Python",
    b"setup.py": "Python",
    b"setup.cfg": "Python",
    b"requirements.txt": "Python",
    b"Pipfile": "Python",
    b"Cargo.toml": "Rust",
    b"go.mod": "Go",
    b"pom.xml": "Java / Kotlin",
    b"build.gradle": "Java / Kotlin",
    b"build.gradle.kts": "Java / Kotlin",
    b"settings.gradle": "Java / Kotlin",
    b"CMakeLists.txt": "C / C++",
    b"Makefile": "C / C++",
    b"meson.build": "C / C++",
    b"configure.ac": "C / C++",
    b"composer.json": "PHP",
    b"Gemfile": "Ruby",
    b"pubspec.yaml": "Dart / Flutter",
    b"mix.exs": "Elixir",
    b"stack.yaml": "Haskell",
    b"Package.swift": "Swift",
    b"Podfile": "Swift",
    b"project.godot": "Godot",
    b".git": "Dépôt git",
}
EXTENSION_MARKERS: dict[str, str] = {
    "csproj": ".NET",
    "fsproj": ".NET",
    "vbproj": ".NET",
    "sln": ".NET",
    "uproject": "Unreal Engine",
    "cabal": "Haskell",
}
# A folder holding one of these folders is a piece of software.
SOFTWARE_CHILDREN: dict[bytes, str] = {
    b"steamapps": "Bibliothèque Steam",
    b"drive_c": "Préfixe Wine",
    b"Program Files": "Copie de Windows",
    b"Program Files (x86)": "Copie de Windows",
}
UNITY = (b"Assets", b"ProjectSettings")
# Often found loose in folders that are no project (a backup with a
# requirements.txt next to photos): alone, they make a project only of a
# folder that holds no other project or program (found on a real disk, D-049).
WEAK_MARKERS = frozenset({b"requirements.txt", b"Makefile"})
# Nothing is looked for inside these: dependencies and tool caches hold
# thousands of package.json and setup.py that are not the user's projects.
DEPENDENCY_DIRS = frozenset(
    {
        b"node_modules",
        b"bower_components",
        b"vendor",
        b"venv",
        b"site-packages",
        b"dist-packages",
        b"__pypackages__",
        b"Pods",
        b"Carthage",
        b"deps",
        b"_build",
        b"target",
        b"pkg",
    }
)
_CHUNK = 500


@dataclass(frozen=True, slots=True)
class Found:
    entry_id: int
    rel_path: bytes
    rel_path_display: str
    name: str
    group: str
    """PROJECT or SOFTWARE."""
    kinds: tuple[str, ...]
    is_dir: bool
    size: int | None
    """Place used on the disk (None if the folder was not fully read)."""
    files: int
    last_change_ns: int | None
    """Latest modification of its own content, without its recreatable folders."""
    recreatable: tuple[Finding, ...] = ()
    nested: int = 0
    """Projects or programs found inside it (a monorepo's packages...)."""

    @property
    def recreatable_size(self) -> int:
        return sum(f.allocated_size or 0 for f in self.recreatable)


@dataclass
class _Candidate:
    kinds: set[str] = field(default_factory=set)
    group: str = PROJECT
    strong: bool = False
    """False when only weak markers (WEAK_MARKERS) make it a project."""


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> Iterable[sqlite3.Row]:
    return conn.execute(sql, params)


def _candidates(conn: sqlite3.Connection, scan_id: int) -> dict[int, _Candidate]:
    found: dict[int, _Candidate] = defaultdict(_Candidate)
    names = [*MARKERS, *SOFTWARE_CHILDREN, *UNITY]
    marks = ",".join("?" * len(names))
    by_parent: dict[int, set[bytes]] = defaultdict(set)
    for row in _rows(
        conn,
        f"SELECT parent_id, name FROM entry WHERE scan_id = ? AND name IN ({marks})",  # noqa: S608
        (scan_id, *names),
    ):
        if row["parent_id"] is not None:
            by_parent[row["parent_id"]].add(bytes(row["name"]))
    for parent, present in by_parent.items():
        for name in present:
            if name in MARKERS:
                found[parent].kinds.add(MARKERS[name])
                found[parent].strong |= name not in WEAK_MARKERS
            elif name in SOFTWARE_CHILDREN:
                found[parent].kinds.add(SOFTWARE_CHILDREN[name])
                found[parent].group = SOFTWARE
        if all(name in present for name in UNITY):
            found[parent].kinds.add("Unity")
            found[parent].strong = True
    extensions = [*EXTENSION_MARKERS, "exe", "dll", "appimage"]
    marks = ",".join("?" * len(extensions))
    windows: dict[int, set[str]] = defaultdict(set)
    appimages: list[int] = []
    for row in _rows(
        conn,
        "SELECT entry_id, parent_id, extension FROM entry WHERE scan_id = ? AND kind = 'file' "  # noqa: S608
        f"AND extension IN ({marks})",
        (scan_id, *extensions),
    ):
        parent, extension = row["parent_id"], row["extension"]
        if extension in EXTENSION_MARKERS:
            found[parent].kinds.add(EXTENSION_MARKERS[extension])
            found[parent].strong = True
        elif extension == "appimage":
            appimages.append(row["entry_id"])
        else:
            windows[parent].add(extension)
    for parent, extensions_here in windows.items():
        if {"exe", "dll"} <= extensions_here:
            found[parent].kinds.add("Programme Windows")
            found[parent].group = SOFTWARE
    for row in _rows(
        conn,
        "SELECT parent_id FROM entry WHERE scan_id = ? AND name >= ? AND name < ?",
        (scan_id, b"goggame-", b"goggame."),
    ):
        found[row["parent_id"]].kinds.add("Jeu GOG")
        found[row["parent_id"]].group = SOFTWARE
    for entry_id in appimages:
        found[entry_id].kinds.add("AppImage")
        found[entry_id].group = SOFTWARE
    for row in _rows(
        conn,
        "SELECT entry_id FROM entry WHERE scan_id = ? AND kind = 'dir' "
        "AND name_display LIKE '%.app'",
        (scan_id,),
    ):
        found[row["entry_id"]].kinds.add("Application macOS")
        found[row["entry_id"]].group = SOFTWARE
    found.pop(None, None)  # type: ignore[call-overload]  # the root has no parent
    return found


def _holds_another(paths: list[bytes], rel_path: bytes) -> bool:
    """Another candidate lies below ``rel_path`` (``paths`` sorted)."""
    if not rel_path:
        return len(paths) > 1
    prefix = rel_path + b"/"
    index = bisect_left(paths, prefix)
    return index < len(paths) and paths[index].startswith(prefix)


def _ancestors(rel_path: bytes) -> Iterable[bytes]:
    """Every folder above, up to the root (b"")."""
    while rel_path:
        rel_path = rel_path.rpartition(b"/")[0]
        yield rel_path


def _hidden_away(rel_path: bytes, group: str) -> bool:
    """Inside a dependency folder, or a hidden one for a project.

    Hidden folders hold tool caches full of other people's packages; software
    often lives in one (~/.local/share/Steam, ~/.wine), so it is kept there.
    """
    parts = rel_path.split(b"/") if rel_path else []
    if any(part in DEPENDENCY_DIRS for part in parts):
        return True
    return group == PROJECT and any(part.startswith(b".") for part in parts)


_ENTRY_SQL = (
    "SELECT e.entry_id, e.rel_path, e.rel_path_display, e.name, e.name_display, e.kind, "
    "e.depth, e.allocated_size, e.mtime_ns, d.allocated_size AS subtree, d.files AS sub_files "
    "FROM entry e LEFT JOIN dir_summary d ON d.scan_id = e.scan_id AND d.entry_id = e.entry_id "
    "WHERE e.scan_id = ? AND e.entry_id IN ({marks})"
)

# The latest change of a folder's own content: walk down by the parent index
# (forced, see organize/plan.py), without the recreatable folders.
_LAST_CHANGE_SQL = (
    "WITH RECURSIVE sub(id) AS ("
    "  SELECT ? UNION ALL"
    "  SELECT e.entry_id FROM sub CROSS JOIN entry e INDEXED BY entry_by_parent"
    "  ON e.scan_id = ? AND e.parent_id = sub.id"
    "  WHERE e.kind = 'dir' AND e.entry_id NOT IN (SELECT value FROM json_each(?))"
    ") "
    "SELECT MAX(f.mtime_ns) FROM sub CROSS JOIN entry f INDEXED BY entry_by_parent "
    "ON f.scan_id = ? AND f.parent_id = sub.id WHERE f.kind = 'file'"
)


def find_projects(
    conn: sqlite3.Connection, scan_id: int, *, min_confidence: int = DEFAULT_MIN_CONFIDENCE
) -> list[Found]:
    """The projects and software of an analysis, outermost ones only, biggest first.

    Their recreatable folders are the cleanup findings (D-023) at ``min_confidence``,
    with the same numbers as on the cleanup page.
    """
    candidates = _candidates(conn, scan_id)
    ids = sorted(candidates)
    rows: dict[int, sqlite3.Row] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        sql = _ENTRY_SQL.format(marks=",".join("?" * len(chunk)))
        for row in conn.execute(sql, (scan_id, *chunk)):
            rows[row["entry_id"]] = row
    eligible = sorted(
        (bytes(rows[i]["rel_path"]), i)
        for i in rows
        if not _hidden_away(bytes(rows[i]["rel_path"]), candidates[i].group)
    )
    paths = [path for path, _ in eligible]
    kept: dict[bytes, int] = {}
    nested: dict[int, int] = defaultdict(int)
    for rel_path, entry_id in eligible:
        candidate = candidates[entry_id]
        if candidate.group == PROJECT and not candidate.strong and _holds_another(paths, rel_path):
            continue  # a folder of projects, not a project (D-049)
        # A program stays visible inside a project (a portable application
        # kept in a backup); everything else is hidden by what holds it.
        outer = next(
            (
                kept[a]
                for a in _ancestors(rel_path)
                if a in kept
                and (candidate.group == PROJECT or candidates[kept[a]].group == SOFTWARE)
            ),
            None,
        )
        if outer is not None:
            nested[outer] += 1
            continue
        kept[rel_path] = entry_id
    recreatable: dict[int, list[Finding]] = defaultdict(list)
    for finding in parasite_findings(conn, scan_id, min_confidence=min_confidence):
        owner = next((kept[a] for a in _ancestors(finding.rel_path) if a in kept), None)
        if owner is not None:
            recreatable[owner].append(finding)
    results: list[Found] = []
    for rel_path, entry_id in kept.items():
        row = rows[entry_id]
        candidate = candidates[entry_id]
        inside = tuple(recreatable[entry_id])
        is_dir = row["kind"] == "dir"
        if is_dir:
            subtree = row["subtree"]
            size = None if subtree is None else subtree + row["allocated_size"]
            excluded = json.dumps([f.entry_id for f in inside])
            last = conn.execute(
                _LAST_CHANGE_SQL, (entry_id, scan_id, excluded, scan_id)
            ).fetchone()[0]
        else:
            size, last = row["allocated_size"], row["mtime_ns"]
        results.append(
            Found(
                entry_id=entry_id,
                rel_path=rel_path,
                rel_path_display=row["rel_path_display"],
                name=row["name_display"],
                group=candidate.group,
                kinds=tuple(sorted(candidate.kinds)),
                is_dir=is_dir,
                size=size,
                files=row["sub_files"] or (0 if is_dir else 1),
                last_change_ns=last,
                recreatable=inside,
                nested=nested[entry_id],
            )
        )
    results.sort(key=lambda f: (-(f.size or 0), f.rel_path))
    return results
