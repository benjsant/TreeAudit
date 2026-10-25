"""Read-only queries for the pages (space explorer, lists)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from treeaudit.core.sizes import is_sparse, shown_file_size

MAX_CHILDREN = 300


@dataclass(frozen=True, slots=True)
class Node:
    entry_id: int
    parent_id: int | None
    name: str
    rel_path: str
    raw_path: bytes
    kind: str
    size: int | None
    """Files: their size. Folders: space used by their subtree (None if not fully scanned)."""
    files: int | None
    mtime_ns: int
    apparent: int | None = None
    """A sparse file's apparent size (``size`` is then the place it uses, D-047)."""


_NODE_SQL = (
    "SELECT e.entry_id, e.parent_id, e.name_display, e.rel_path_display, e.rel_path, e.kind, "
    "e.size, e.allocated_size, e.mtime_ns, d.allocated_size AS subtree, "
    "d.files AS subtree_files "
    "FROM entry e LEFT JOIN dir_summary d ON d.scan_id = e.scan_id AND d.entry_id = e.entry_id "
)


def _node(row: sqlite3.Row) -> Node:
    if row["kind"] == "dir":
        size = None if row["subtree"] is None else row["subtree"] + row["allocated_size"]
        files = row["subtree_files"]
        sparse = None
    else:
        size, files = shown_file_size(row["size"], row["allocated_size"]), None
        sparse = row["size"] if is_sparse(row["size"], row["allocated_size"]) else None
    return Node(
        entry_id=row["entry_id"],
        parent_id=row["parent_id"],
        name=row["name_display"],
        rel_path=row["rel_path_display"],
        raw_path=bytes(row["rel_path"]),
        kind=row["kind"],
        size=size,
        files=files,
        mtime_ns=row["mtime_ns"],
        apparent=sparse,
    )


def start_node(conn: sqlite3.Connection, scan_id: int) -> Node | None:
    row = conn.execute(_NODE_SQL + "WHERE e.scan_id = ? AND e.depth = 0", (scan_id,)).fetchone()
    return None if row is None else _node(row)


def get_node(conn: sqlite3.Connection, scan_id: int, entry_id: int) -> Node | None:
    row = conn.execute(
        _NODE_SQL + "WHERE e.scan_id = ? AND e.entry_id = ?", (scan_id, entry_id)
    ).fetchone()
    return None if row is None else _node(row)


def all_children(conn: sqlite3.Connection, scan_id: int, entry_id: int) -> list[Node]:
    rows = conn.execute(
        _NODE_SQL + "WHERE e.scan_id = ? AND e.parent_id = ?", (scan_id, entry_id)
    ).fetchall()
    return [_node(row) for row in rows]


def children(
    conn: sqlite3.Connection, scan_id: int, entry_id: int, limit: int = MAX_CHILDREN
) -> tuple[list[Node], int]:
    """Children of a folder, largest first, and their total number."""
    nodes = all_children(conn, scan_id, entry_id)
    nodes.sort(key=lambda n: (-(n.size or 0), n.name))
    return nodes[:limit], len(nodes)


def breadcrumb(conn: sqlite3.Connection, scan_id: int, node: Node) -> list[Node]:
    """Ancestors of a node, from the start of the scan down to the node itself."""
    chain = [node]
    current = node
    while current.parent_id is not None:
        parent = get_node(conn, scan_id, current.parent_id)
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return list(reversed(chain))
