"""File explorer and basket (D-036): read-only views of an analysis.

Everything comes from the index of a finished analysis: the disk is never read
again, so browsing a slow USB disk stays instant. Marks (folder to clean,
duplicate, protected) reuse the parasite rules and the duplicate analyses.

The basket holds entry numbers only. Paths are always read back from the index
here, so a page cannot make a command target a path of its own choosing.
"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from treeaudit.duplicates.groups import protection_reason, repo_dirs_of
from treeaudit.parasites.cached import parasite_findings
from treeaudit.parasites.detect import Finding
from treeaudit.shellcmd import is_displayable
from treeaudit.web.queries import Node

PAGE_SIZE = 200
MAX_COPIES_SHOWN = 50
_CHUNK = 500  # SQLite parameters per query, well under its limit


class Sort(StrEnum):
    SIZE = "size"
    NAME = "name"
    MTIME = "mtime"


SORT_LABELS = {Sort.SIZE: "taille", Sort.NAME: "nom", Sort.MTIME: "date"}


def sort_nodes(nodes: list[Node], sort: Sort) -> list[Node]:
    """Folders first when sorting by name, as in a file manager."""
    if sort is Sort.NAME:
        return sorted(nodes, key=lambda n: (n.kind != "dir", n.name.casefold(), n.name))
    if sort is Sort.MTIME:
        return sorted(nodes, key=lambda n: (-n.mtime_ns, n.name))
    return sorted(nodes, key=lambda n: (-(n.size or 0), n.name))


@dataclass(frozen=True, slots=True)
class Marks:
    """What TreeAudit knows about one entry, shown as badges."""

    parasite: Finding | None = None
    copies: int = 0
    """Number of identical files (this one included) in the last duplicate analysis."""
    protection: str | None = None

    @property
    def any(self) -> bool:
        return self.parasite is not None or self.copies > 1 or self.protection is not None


def latest_dupes_run(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row | None:
    """The most recent finished search for duplicates (>= 1 KiB) of this scan."""
    row = conn.execute(
        "SELECT * FROM dup_run WHERE scan_id = ? AND status IN ('complete', 'incomplete') "
        "AND json_extract(options, '$.micro') IS NOT 1 ORDER BY id DESC LIMIT 1",
        (scan_id,),
    ).fetchone()
    return row if isinstance(row, sqlite3.Row) else None


def _chunks(values: Sequence[int]) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _copy_counts(
    conn: sqlite3.Connection, scan_id: int, run_id: int, entry_ids: Sequence[int]
) -> dict[int, int]:
    """Entry -> number of identical files. Hard links share the digest of their inode."""
    counts: dict[int, int] = {}
    for chunk in _chunks(entry_ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT c.entry_id, "  # noqa: S608 - only "?" placeholders are added
            "(SELECT COUNT(*) FROM file_hash o WHERE o.run_id = h.run_id AND o.size = h.size "
            "AND o.full = h.full) AS copies "
            "FROM entry c "
            "JOIN entry r ON r.scan_id = c.scan_id AND r.dev = c.dev AND r.ino = c.ino "
            "JOIN file_hash h ON h.run_id = ? AND h.entry_id = r.entry_id AND h.full IS NOT NULL "
            f"WHERE c.scan_id = ? AND c.kind = 'file' AND c.entry_id IN ({marks})",
            (run_id, scan_id, *chunk),
        )
        for row in rows:
            counts[row["entry_id"]] = max(counts.get(row["entry_id"], 0), row["copies"])
    return counts


_HELD = {
    "treeaudit": "contient TreeAudit lui-même (son index, ses archives)",
    "private": "contient des dossiers privés jamais analysés (clés, profils de navigateur…)",
}


def _holders(conn: sqlite3.Connection, scan_id: int) -> dict[bytes, str]:
    """Folders containing TreeAudit's own or private folders (D-037), and why.

    Those were left out of the analysis: removing a folder that holds them would
    remove them too, unseen.
    """
    holders: dict[bytes, str] = {}
    rows = conn.execute(
        "SELECT rel_path, reason FROM scan_exclusion WHERE scan_id = ? AND reason IN (?, ?) "
        "ORDER BY reason",  # "treeaudit" comes last: it wins when both apply
        (scan_id, *_HELD),
    )
    for row in rows:
        for ancestor in (b"", *_ancestors(bytes(row["rel_path"]))):
            holders[ancestor] = _HELD[row["reason"]]
    return holders


class MarkSource:
    """Loads, once per page, what is needed to mark entries of one scan."""

    def __init__(self, conn: sqlite3.Connection, scan_id: int) -> None:
        self._conn = conn
        self._scan_id = scan_id
        self.findings = {f.entry_id: f for f in parasite_findings(conn, scan_id)}
        self.repo_dirs = repo_dirs_of(conn, scan_id)
        self.dupes_run = latest_dupes_run(conn, scan_id)
        self.holders = _holders(conn, scan_id)

    def protection(self, rel_path: bytes, kind: str) -> str | None:
        held = self.holders.get(rel_path)
        if held is not None:
            return held
        if kind == "dir" and rel_path in self.repo_dirs:
            return "dépôt git"
        return protection_reason(rel_path, self.repo_dirs)

    def marks(self, entries: Sequence[tuple[int, bytes, str]]) -> dict[int, Marks]:
        """Marks of (entry id, raw relative path, kind) entries."""
        copies: dict[int, int] = {}
        files = [entry_id for entry_id, _, kind in entries if kind == "file"]
        if self.dupes_run is not None and files:
            copies = _copy_counts(self._conn, self._scan_id, self.dupes_run["id"], files)
        result: dict[int, Marks] = {}
        for entry_id, rel_path, kind in entries:
            finding = self.findings.get(entry_id)
            # A folder to clean is often inside a project: "protected" would only confuse.
            protection = self.holders.get(rel_path)
            if protection is None and finding is None:
                protection = self.protection(rel_path, kind)
            result[entry_id] = Marks(finding, copies.get(entry_id, 0), protection)
        return result


# --- details of one entry ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Copy:
    entry_id: int
    rel_path_display: str


@dataclass(frozen=True, slots=True)
class Details:
    entry_id: int
    parent_id: int | None
    depth: int
    name: str
    rel_path: bytes
    rel_path_display: str
    kind: str
    size: int
    allocated_size: int
    mtime_ns: int
    permissions: str
    uid: int
    gid: int
    hard_links: int
    link_target: str | None
    readable_name: bool
    """False when the path cannot be shown or quoted unambiguously (no command)."""
    subtree: sqlite3.Row | None
    marks: Marks
    other_copies: tuple[Copy, ...]


def entry_details(conn: sqlite3.Connection, scan_id: int, entry_id: int) -> Details | None:
    row = conn.execute(
        "SELECT * FROM entry WHERE scan_id = ? AND entry_id = ?", (scan_id, entry_id)
    ).fetchone()
    if row is None:
        return None
    rel_path = bytes(row["rel_path"])
    source = MarkSource(conn, scan_id)
    marks = source.marks([(entry_id, rel_path, row["kind"])])[entry_id]
    subtree = None
    if row["kind"] == "dir":
        subtree = conn.execute(
            "SELECT * FROM dir_summary WHERE scan_id = ? AND entry_id = ?", (scan_id, entry_id)
        ).fetchone()
    copies: tuple[Copy, ...] = ()
    if marks.copies > 1 and source.dupes_run is not None:
        copies = _other_copies(conn, scan_id, source.dupes_run["id"], row)
    target = row["link_target"]
    return Details(
        entry_id=entry_id,
        parent_id=row["parent_id"],
        depth=row["depth"],
        name=row["name_display"],
        rel_path=rel_path,
        rel_path_display=row["rel_path_display"],
        kind=row["kind"],
        size=row["size"],
        allocated_size=row["allocated_size"],
        mtime_ns=row["mtime_ns"],
        permissions=stat.filemode(row["mode"]),
        uid=row["uid"],
        gid=row["gid"],
        hard_links=row["nlink"],
        link_target=None if target is None else bytes(target).decode("utf-8", "backslashreplace"),
        readable_name=is_displayable(rel_path),
        subtree=subtree,
        marks=marks,
        other_copies=copies,
    )


def _other_copies(
    conn: sqlite3.Connection, scan_id: int, run_id: int, entry: sqlite3.Row
) -> tuple[Copy, ...]:
    rows = conn.execute(
        "SELECT DISTINCT e.entry_id, e.rel_path_display FROM entry me "
        "JOIN entry r ON r.scan_id = me.scan_id AND r.dev = me.dev AND r.ino = me.ino "
        "JOIN file_hash h ON h.run_id = ? AND h.entry_id = r.entry_id AND h.full IS NOT NULL "
        "JOIN file_hash o ON o.run_id = h.run_id AND o.size = h.size AND o.full = h.full "
        "JOIN entry oe ON oe.scan_id = me.scan_id AND oe.entry_id = o.entry_id "
        "JOIN entry e ON e.scan_id = me.scan_id AND e.dev = oe.dev AND e.ino = oe.ino "
        "AND e.kind = 'file' "
        "WHERE me.scan_id = ? AND me.entry_id = ? AND e.entry_id != me.entry_id "
        "ORDER BY e.rel_path LIMIT ?",
        (run_id, scan_id, entry["entry_id"], MAX_COPIES_SHOWN),
    )
    return tuple(Copy(r["entry_id"], r["rel_path_display"]) for r in rows)


# --- basket ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BasketItem:
    entry_id: int
    rel_path: bytes
    rel_path_display: str
    kind: str
    allocated: int | None
    """Space used on disk (whole subtree for a folder; None if not fully scanned)."""
    protection: str | None


@dataclass(frozen=True, slots=True)
class Basket:
    items: tuple[BasketItem, ...]
    unknown: tuple[int, ...]
    """Numbers that are not entries of this analysis (another scan, stale page)."""
    covered: tuple[int, ...]
    """Entries left out because a selected folder already contains them."""
    root_refused: bool
    """The analysed folder itself was selected: never offered as a whole."""

    @property
    def total(self) -> int:
        return sum(item.allocated or 0 for item in self.items)

    @property
    def unknown_size(self) -> bool:
        return any(item.allocated is None for item in self.items)


def _ancestors(rel_path: bytes) -> Iterable[bytes]:
    while b"/" in rel_path:
        rel_path = rel_path.rpartition(b"/")[0]
        yield rel_path


def resolve_basket(conn: sqlite3.Connection, scan_id: int, entry_ids: Sequence[int]) -> Basket:
    wanted = sorted(set(entry_ids))
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(wanted):
        marks = ",".join("?" * len(chunk))
        rows.extend(
            conn.execute(
                "SELECT e.entry_id, e.depth, e.rel_path, e.rel_path_display, e.kind, "  # noqa: S608
                "e.allocated_size, d.allocated_size AS subtree "
                "FROM entry e LEFT JOIN dir_summary d "
                "ON d.scan_id = e.scan_id AND d.entry_id = e.entry_id "
                f"WHERE e.scan_id = ? AND e.entry_id IN ({marks})",
                (scan_id, *chunk),
            )
        )
    found = {row["entry_id"] for row in rows}
    unknown = tuple(entry_id for entry_id in wanted if entry_id not in found)
    root_refused = any(row["depth"] == 0 for row in rows)
    rows = [row for row in rows if row["depth"] > 0]
    selected_dirs = {bytes(row["rel_path"]) for row in rows if row["kind"] == "dir"}
    source = MarkSource(conn, scan_id)
    items: list[BasketItem] = []
    covered: list[int] = []
    for row in sorted(rows, key=lambda r: bytes(r["rel_path"])):
        rel_path = bytes(row["rel_path"])
        if any(parent in selected_dirs for parent in _ancestors(rel_path)):
            covered.append(row["entry_id"])
            continue
        if row["kind"] == "dir":
            subtree = row["subtree"]
            allocated = None if subtree is None else subtree + row["allocated_size"]
        else:
            allocated = row["allocated_size"]
        protection = source.holders.get(rel_path)
        if protection is None and row["entry_id"] not in source.findings:
            protection = source.protection(rel_path, row["kind"])
        items.append(
            BasketItem(
                entry_id=row["entry_id"],
                rel_path=rel_path,
                rel_path_display=row["rel_path_display"],
                kind=row["kind"],
                allocated=allocated,
                protection=protection,
            )
        )
    return Basket(tuple(items), unknown, tuple(covered), root_refused)
