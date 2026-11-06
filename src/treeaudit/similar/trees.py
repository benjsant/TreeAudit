"""Folders that are (almost) the same, found and compared from the index (D-058).

Read from the index and a finished search for duplicates only: nothing on the
disk is read, nothing is changed.

Finding pairs: every file with an identical copy elsewhere is paired with that
copy; both paths lose the file names, then the folders they share at the end
("2019/Noël" in "Sauvegarde/2019/Noël" and "Sauvegarde (copie)/2019/Noël").
What is left is a pair of folders, credited with the bytes of the file. The
pairs with the most bytes in common are then compared in full.

Comparing two folders A and B, file by file, by their path below A and B:
- identical at the same place, identical elsewhere (moved or renamed),
  modified (same place, other content), only in A, only in B: facts, from
  digests or from a size no other file has;
- same place and size but content never read (files below the search's
  minimum size, read errors): a supposition, shown apart.
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from treeaudit.db.connection import from_sqlite_int

MAX_PAIRS = 50
"""Pairs compared in full, the most bytes in common first."""
MAX_GROUP_PATHS = 50
"""Paths of one content used to find pairs: a file copied everywhere (a
licence, an icon) would otherwise make thousands of pairs."""
MIN_SHARED_FILES = 2
MIN_SCORE = 0.5
MAX_FILES = 300_000
"""Beyond this, one side of a pair is too big to compare here."""
SHOWN = 1000
"""Elements kept per list; the totals always count everything."""


class TooLargeError(ValueError):
    """One of the folders holds more than MAX_FILES files."""


# Content of a file, as far as the search for duplicates knows it.
# ("h", size, digest): read; ("u", dev, ino): no other file has this content;
# ("e",): empty; ("?", size): never read (supposition: compared by size).
Key = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class Item:
    path: str
    """Path below the compared folder."""
    size: int


@dataclass(frozen=True, slots=True)
class Moved:
    path_a: str
    path_b: str
    size: int


@dataclass
class Listing:
    """Up to SHOWN elements, with the true totals."""

    items: list[Item | Moved] = field(default_factory=list)
    count: int = 0
    size: int = 0

    def add(self, item: Item | Moved) -> None:
        self.count += 1
        self.size += item.size
        if len(self.items) < SHOWN:
            self.items.append(item)


@dataclass
class Comparison:
    a_id: int
    b_id: int
    a_path: str
    b_path: str
    files_a: int = 0
    files_b: int = 0
    bytes_a: int = 0
    bytes_b: int = 0
    same: Listing = field(default_factory=Listing)
    moved: Listing = field(default_factory=Listing)
    modified: Listing = field(default_factory=Listing)
    only_a: Listing = field(default_factory=Listing)
    only_b: Listing = field(default_factory=Listing)
    unverified: Listing = field(default_factory=Listing)
    """Same place, same size, content never read: a supposition."""

    def found_in_other(self, side: str) -> float:
        """Share of one side found identical in the other, among its files
        whose content is known (facts only; suppositions are shown apart).

        The lower of two shares, bytes and files: a few big files in common
        do not make a folder a copy, nor many tiny ones."""
        files = (self.files_a if side == "a" else self.files_b) - self.unverified.count
        size = (self.bytes_a if side == "a" else self.bytes_b) - self.unverified.size
        if files <= 0:
            return 0.0
        by_files = (self.same.count + self.moved.count) / files
        by_bytes = (self.same.size + self.moved.size) / size if size > 0 else by_files
        return min(by_files, by_bytes)

    @property
    def score(self) -> float:
        """How much of one folder is in the other, for the side most inside."""
        return max(self.found_in_other("a"), self.found_in_other("b"))

    def contains(self, side: str) -> bool:
        """Every file of the other side is identical somewhere on this side (fact)."""
        missing = self.only_a if side == "b" else self.only_b
        return missing.count == 0 and self.modified.count == 0 and self.unverified.count == 0


@dataclass(frozen=True, slots=True)
class _Run:
    id: int
    min_size: int
    max_size: int | None
    complete: bool


@dataclass
class _Knowledge:
    """What the searches for duplicates learnt, by inode."""

    runs: tuple[_Run, ...]
    """The search, then the small-files search if there was one."""
    digests: dict[tuple[int, int], bytes | None]
    """Inodes read: their digest, or None when no other file of that size
    starts and ends like them (unique content)."""
    errors: set[tuple[int, int]]

    def key(self, size: int, dev: int, ino: int) -> Key:
        inode = (dev, ino)
        if size == 0:
            return ("e",)
        if inode in self.errors:
            return ("?", size)
        if inode in self.digests:
            digest = self.digests[inode]
            return ("u", dev, ino) if digest is None else ("h", size, digest)
        for run in self.runs:
            below = size < run.min_size
            above = run.max_size is not None and size > run.max_size
            if not below and not above:
                # Within the sizes this search covered, not read: no other file
                # has this size, so its content is unique (if the search ended).
                return ("u", dev, ino) if run.complete else ("?", size)
        return ("?", size)


def latest_run(conn: sqlite3.Connection, scan_id: int, *, micro: bool = False) -> _Run | None:
    """The latest finished search for duplicates (or for small duplicates)."""
    row = conn.execute(
        "SELECT id, status, options FROM dup_run WHERE scan_id = ? "
        "AND status IN ('complete', 'incomplete') "
        "AND IFNULL(json_extract(options, '$.micro'), 0) = ? "
        "ORDER BY id DESC LIMIT 1",
        (scan_id, 1 if micro else 0),
    ).fetchone()
    if row is None:
        return None
    options = json.loads(row["options"])
    max_size = options.get("max_size")
    return _Run(
        id=row["id"],
        min_size=int(options.get("min_size") or 1),
        max_size=None if max_size is None else int(max_size),
        complete=row["status"] == "complete",
    )


def knowledge_of(conn: sqlite3.Connection, scan_id: int, run: _Run) -> _Knowledge:
    """Digests of the search, and of the small-files search when there was one."""
    small = latest_run(conn, scan_id, micro=True)
    runs = (run,) if small is None else (run, small)
    digests: dict[tuple[int, int], bytes | None] = {}
    errors: set[tuple[int, int]] = set()
    for each in runs:
        rows = conn.execute(
            "SELECT r.dev, r.ino, h.full FROM file_hash h "
            "JOIN entry r ON r.scan_id = ? AND r.entry_id = h.entry_id WHERE h.run_id = ?",
            (scan_id, each.id),
        )
        for row in rows:
            full = row["full"]
            digests[(from_sqlite_int(row["dev"]), from_sqlite_int(row["ino"]))] = (
                None if full is None else bytes(full)
            )
        errors |= {
            (from_sqlite_int(row["dev"]), from_sqlite_int(row["ino"]))
            for row in conn.execute(
                "SELECT r.dev, r.ino FROM dup_error d "
                "JOIN entry r ON r.scan_id = ? AND r.entry_id = d.entry_id WHERE d.run_id = ?",
                (scan_id, each.id),
            )
        }
    return _Knowledge(runs, digests, errors)


# --- finding pairs -------------------------------------------------------------------


def _folders(path_a: bytes, path_b: bytes) -> tuple[bytes, bytes] | None:
    """The pair of folders two copies of one file stand for, or None.

    The file names go, then the folders both paths end with, as long as
    neither folder becomes one of the other's parents: "Photos" and
    "Sauvegarde/Photos" stay a pair, not the analysed folder and "Sauvegarde".
    A folder and one of its own sub-folders make no pair."""

    def nested(x: list[bytes], y: list[bytes]) -> bool:
        return x == y[: len(x)] or y == x[: len(y)]

    a = path_a.split(b"/")[:-1]
    b = path_b.split(b"/")[:-1]
    while a and b and a[-1] == b[-1] and not nested(a[:-1], b[:-1]):
        a.pop()
        b.pop()
    if nested(a, b):
        return None
    return b"/".join(a), b"/".join(b)


def _copies(
    conn: sqlite3.Connection, scan_id: int, run_id: int
) -> Iterator[tuple[int, list[bytes]]]:
    """(size, paths) of every content held by at least two inodes."""
    rows = conn.execute(
        "SELECT h.size, h.full, e.rel_path FROM file_hash h "
        "JOIN entry r ON r.scan_id = ? AND r.entry_id = h.entry_id "
        "JOIN entry e ON e.scan_id = r.scan_id AND e.dev = r.dev AND e.ino = r.ino "
        "AND e.kind = 'file' "
        "WHERE h.run_id = ? AND h.full IS NOT NULL AND (h.size, h.full) IN ("
        "  SELECT size, full FROM file_hash WHERE run_id = ? AND full IS NOT NULL "
        "  GROUP BY size, full HAVING COUNT(*) >= 2"
        ") ORDER BY h.size, h.full",
        (scan_id, run_id, run_id),
    )
    current: tuple[int, bytes] | None = None
    paths: list[bytes] = []
    for row in rows:
        content = (row["size"], bytes(row["full"]))
        if content != current:
            if current is not None:
                yield current[0], paths
            current, paths = content, []
        paths.append(bytes(row["rel_path"]))
    if current is not None:
        yield current[0], paths


def candidate_pairs(
    conn: sqlite3.Connection, scan_id: int, run_id: int
) -> list[tuple[bytes, bytes, int, int]]:
    """(folder A, folder B, bytes, files) in common, the most bytes first."""
    shared: dict[tuple[bytes, bytes], list[int]] = defaultdict(lambda: [0, 0])
    for size, paths in _copies(conn, scan_id, run_id):
        chosen = sorted(paths)[:MAX_GROUP_PATHS]
        seen: set[tuple[bytes, bytes]] = set()
        for i, first in enumerate(chosen):
            for second in chosen[i + 1 :]:
                pair = _folders(first, second)
                if pair is None:
                    continue
                pair = (min(pair), max(pair))
                if pair in seen:  # one content counts once per pair
                    continue
                seen.add(pair)
                total = shared[pair]
                total[0] += size
                total[1] += 1
    found = [(a, b, t[0], t[1]) for (a, b), t in shared.items() if t[1] >= MIN_SHARED_FILES]
    return heapq.nlargest(MAX_PAIRS * 4, found, key=lambda p: (p[2], p[3], p[0], p[1]))


# --- comparing a pair ----------------------------------------------------------------


_SUBTREE_SQL = (
    "WITH RECURSIVE sub(id) AS ("
    "  SELECT ? UNION ALL"
    "  SELECT e.entry_id FROM sub CROSS JOIN entry e INDEXED BY entry_by_parent"
    "  ON e.scan_id = ? AND e.parent_id = sub.id"
    ") "
    "SELECT e.rel_path, e.rel_path_display, e.size, e.dev, e.ino "
    "FROM sub CROSS JOIN entry e ON e.scan_id = ? AND e.entry_id = sub.id "
    "WHERE e.kind = 'file' LIMIT ?"
)


def _files(
    conn: sqlite3.Connection, scan_id: int, folder_id: int, knowledge: _Knowledge
) -> tuple[str, dict[bytes, tuple[str, int, Key]]]:
    """(folder path, below-path -> (shown path, size, content key))."""
    head = conn.execute(
        "SELECT rel_path, rel_path_display FROM entry WHERE scan_id = ? AND entry_id = ?",
        (scan_id, folder_id),
    ).fetchone()
    if head is None:
        raise KeyError(folder_id)
    prefix = bytes(head["rel_path"])
    shown_prefix = head["rel_path_display"]
    cut = len(prefix) + 1 if prefix else 0
    shown_cut = len(shown_prefix) + 1 if shown_prefix else 0
    rows = conn.execute(_SUBTREE_SQL, (folder_id, scan_id, scan_id, MAX_FILES + 1)).fetchall()
    if len(rows) > MAX_FILES:
        raise TooLargeError(len(rows))
    files = {}
    for row in rows:
        size = row["size"]
        key = knowledge.key(size, from_sqlite_int(row["dev"]), from_sqlite_int(row["ino"]))
        files[bytes(row["rel_path"])[cut:]] = (row["rel_path_display"][shown_cut:], size, key)
    return shown_prefix, files


def _is_fact(key: Key) -> bool:
    return key[0] != "?"


def compare(
    conn: sqlite3.Connection, scan_id: int, a_id: int, b_id: int, knowledge: _Knowledge
) -> Comparison:
    a_path, files_a = _files(conn, scan_id, a_id, knowledge)
    b_path, files_b = _files(conn, scan_id, b_id, knowledge)
    result = Comparison(a_id, b_id, a_path, b_path)
    result.files_a, result.files_b = len(files_a), len(files_b)
    result.bytes_a = sum(size for _, size, _ in files_a.values())
    result.bytes_b = sum(size for _, size, _ in files_b.values())

    left_a: dict[Key, list[tuple[str, int]]] = defaultdict(list)
    left_b: list[tuple[str, int, Key]] = []
    for path in sorted(files_a.keys() | files_b.keys()):
        in_a, in_b = files_a.get(path), files_b.get(path)
        if in_a is not None and in_b is not None:
            shown, size_a, key_a = in_a
            _, size_b, key_b = in_b
            if _is_fact(key_a) and _is_fact(key_b):
                if key_a == key_b:
                    result.same.add(Item(shown, size_a))
                else:
                    result.modified.add(Item(shown, max(size_a, size_b)))
            elif size_a == size_b:
                result.unverified.add(Item(shown, size_a))
            else:  # another size: another content, whatever was read
                result.modified.add(Item(shown, max(size_a, size_b)))
        elif in_a is not None:
            left_a[in_a[2]].append((in_a[0], in_a[1]))
        elif in_b is not None:
            left_b.append(in_b)
    # Moved or renamed: the same content at another place, read (never a supposition).
    for shown, size, key in left_b:
        candidates = left_a.get(key) if _is_fact(key) and key[0] != "e" else None
        if candidates:
            path_a, _ = candidates.pop(0)
            result.moved.add(Moved(path_a, shown, size))
        else:
            result.only_b.add(Item(shown, size))
    for remaining in left_a.values():
        for shown, size in remaining:
            result.only_a.add(Item(shown, size))
    return result


# --- the whole search ----------------------------------------------------------------


def _resolve(conn: sqlite3.Connection, scan_id: int, rel_path: bytes) -> int | None:
    """Entry id of a folder, walked down by names from the analysed folder.

    Paths are relative to the root; an analysis of a sub-folder starts there."""
    row = conn.execute(
        "SELECT entry_id, rel_path FROM entry WHERE scan_id = ? AND depth = 0", (scan_id,)
    ).fetchone()
    if row is None:
        return None
    current = int(row["entry_id"])
    start = bytes(row["rel_path"])
    if start:
        if rel_path == start:
            return current
        if not rel_path.startswith(start + b"/"):
            return None
        rel_path = rel_path[len(start) + 1 :]
    for name in rel_path.split(b"/") if rel_path else []:
        row = conn.execute(
            "SELECT entry_id FROM entry INDEXED BY entry_by_parent "
            "WHERE scan_id = ? AND parent_id = ? AND name = ? AND kind = 'dir'",
            (scan_id, current, name),
        ).fetchone()
        if row is None:
            return None
        current = int(row["entry_id"])
    return current


@dataclass
class Family:
    """Folders (almost) the same as each other, each compared to the biggest."""

    comparisons: list[Comparison]
    """The reference is side A of every comparison."""

    @property
    def reference(self) -> str:
        return self.comparisons[0].a_path

    @property
    def members(self) -> int:
        return len(self.comparisons) + 1

    @property
    def score(self) -> float:
        return min(c.score for c in self.comparisons)


@dataclass
class Report:
    run_id: int
    complete: bool
    pairs: list[Comparison]
    families: list[Family] = field(default_factory=list)
    skipped_large: int = 0
    small_files_read: bool = False

    @property
    def unverified(self) -> int:
        """Files of the families shown whose content was never read."""
        return sum(c.unverified.count for f in self.families for c in f.comparisons)


def _inside(path: str, folder: str) -> bool:
    return folder == "" or path == folder or path.startswith(folder + "/")


def similar_folders(conn: sqlite3.Connection, scan_id: int) -> Report | None:
    """Pairs of folders with at least MIN_SCORE of their content in common.

    None when duplicates were never searched for: nothing can be said."""
    run = latest_run(conn, scan_id)
    if run is None:
        return None
    knowledge = knowledge_of(conn, scan_id, run)
    report = Report(run.id, run.complete, [], small_files_read=len(knowledge.runs) > 1)
    found: list[Comparison] = []
    for folder_a, folder_b, _, _ in candidate_pairs(conn, scan_id, run.id):
        a_id, b_id = _resolve(conn, scan_id, folder_a), _resolve(conn, scan_id, folder_b)
        if a_id is None or b_id is None:
            continue
        try:
            pair = compare(conn, scan_id, a_id, b_id, knowledge)
        except TooLargeError:
            report.skipped_large += 1
            continue
        if pair.score >= MIN_SCORE:
            found.append(pair)
    # A pair inside another kept pair (either way round) says nothing more.
    kept = [
        pair
        for pair in found
        if not any(other is not pair and _within(pair, other) for other in found)
    ]
    kept.sort(key=lambda c: (-c.score, -(c.same.size + c.moved.size), c.a_path, c.b_path))
    report.pairs = kept[:MAX_PAIRS]
    report.families = _families(conn, scan_id, report.pairs, knowledge)
    return report


def _families(
    conn: sqlite3.Connection, scan_id: int, pairs: list[Comparison], knowledge: _Knowledge
) -> list[Family]:
    """Pairs sharing a folder join one family; each member is compared with the
    biggest one (a folder copied 16 times is one family, not 15 pairs)."""
    parent: dict[int, int] = {}

    def root(node: int) -> int:
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    size: dict[int, tuple[int, str]] = {}
    for pair in pairs:
        size[pair.a_id] = (pair.bytes_a, pair.a_path)
        size[pair.b_id] = (pair.bytes_b, pair.b_path)
        parent[root(pair.a_id)] = root(pair.b_id)
    members: dict[int, list[int]] = defaultdict(list)
    for node in size:
        members[root(node)].append(node)
    by_pair = {(p.a_id, p.b_id): p for p in pairs}
    families = []
    for group in members.values():
        group.sort(key=lambda n: (-size[n][0], size[n][1]))
        reference, others = group[0], group[1:]
        comparisons = []
        for other in others:
            known = by_pair.get((reference, other))
            comparisons.append(known or compare(conn, scan_id, reference, other, knowledge))
        families.append(Family(comparisons))
    families.sort(key=lambda f: (-f.score, -f.members, f.reference))
    return families


def _within(pair: Comparison, other: Comparison) -> bool:
    """`pair` lies inside `other`: both its folders below other's two folders."""
    straight = _inside(pair.a_path, other.a_path) and _inside(pair.b_path, other.b_path)
    crossed = _inside(pair.a_path, other.b_path) and _inside(pair.b_path, other.a_path)
    same = (pair.a_path, pair.b_path) == (other.a_path, other.b_path)
    return (straight or crossed) and not same
