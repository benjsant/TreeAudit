"""Searching a scan with a compiled filter (index only, streaming, bounded memory)."""

from __future__ import annotations

import heapq
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from treeaudit.db.connection import from_sqlite_int
from treeaudit.filters.spec import CompiledFilter, EntryKindFilter
from treeaudit.shellcmd import is_displayable

DEFAULT_TIME_BUDGET = 30.0
_CHECK_EVERY = 2000
_SELECT = (
    "SELECT e.entry_id, e.name_display, e.rel_path, e.rel_path_display, e.kind, e.size, "
    "e.mtime_ns, e.dev, e.ino, e.nlink, e.allocated_size, d.allocated_size AS subtree "
    "FROM entry e LEFT JOIN dir_summary d ON d.scan_id = e.scan_id AND d.entry_id = e.entry_id"
)


class Sort(StrEnum):
    SIZE = "size"  # largest first
    PATH = "path"  # alphabetical (byte order)
    MTIME = "mtime"  # most recent first


@dataclass(frozen=True, slots=True)
class Match:
    entry_id: int
    rel_path: bytes
    rel_path_display: str
    kind: str
    size: int | None
    """File size; for a directory, the space used by its subtree when it was fully scanned."""
    mtime_ns: int


@dataclass(slots=True)
class SearchResult:
    total: int = 0
    files: int = 0
    dirs: int = 0
    others: int = 0
    files_size: int = 0
    """Sum of matching file sizes, hard links counted once."""
    not_displayable: int = 0
    rows_examined: int = 0
    partial_reason: str | None = None
    examples: list[Match] = field(default_factory=list)


def _sql(compiled: CompiledFilter) -> tuple[str, list[object]]:
    spec = compiled.spec
    conditions = ["e.scan_id = ?", "e.depth > 0"]
    params: list[object] = []
    kinds = list(spec.kinds)
    size_bounded = spec.min_size is not None or spec.max_size is not None
    if (compiled.extensions or size_bounded) and not kinds:
        kinds = [EntryKindFilter.FILE]  # extensions and sizes only concern files
    if kinds:
        conditions.append(f"e.kind IN ({', '.join('?' * len(kinds))})")
        params.extend(k.value for k in kinds)
    if compiled.extensions:
        conditions.append(f"e.extension IN ({', '.join('?' * len(compiled.extensions))})")
        params.extend(compiled.extensions)
    if spec.min_size is not None:
        conditions.append("(e.kind != 'file' OR e.size >= ?)")
        params.append(spec.min_size)
    if spec.max_size is not None:
        conditions.append("(e.kind != 'file' OR e.size <= ?)")
        params.append(spec.max_size)
    # Only constant fragments are assembled; every value is bound with "?".
    where = " AND ".join(conditions)
    sql = f"{_SELECT} WHERE {where}"
    return sql, params


def search(
    conn: sqlite3.Connection,
    scan_id: int,
    compiled: CompiledFilter,
    *,
    limit: int = 20,
    sort: Sort = Sort.SIZE,
    time_budget: float = DEFAULT_TIME_BUDGET,
    cancel: threading.Event | None = None,
) -> SearchResult:
    """Count every match and keep the ``limit`` best examples for ``sort``."""
    result = SearchResult()
    sql, params = _sql(compiled)
    deadline = time.monotonic() + time_budget
    seen_inodes: set[tuple[int, int]] = set()

    def matches() -> Iterator[Match]:
        for row in conn.execute(sql, [scan_id, *params]):
            result.rows_examined += 1
            if result.rows_examined % _CHECK_EVERY == 0:
                if cancel is not None and cancel.is_set():
                    result.partial_reason = "recherche annulée"
                    return
                if time.monotonic() > deadline:
                    result.partial_reason = f"budget de temps dépassé ({time_budget:.0f} s)"
                    return
            if compiled.needs_text and not compiled.matches(
                row["name_display"], row["rel_path_display"]
            ):
                continue
            kind = row["kind"]
            result.total += 1
            if kind == "file":
                result.files += 1
                inode = (from_sqlite_int(row["dev"]), from_sqlite_int(row["ino"]))
                if row["nlink"] <= 1 or inode not in seen_inodes:
                    result.files_size += row["size"]
                    if row["nlink"] > 1:
                        seen_inodes.add(inode)
                size: int | None = row["size"]
            elif kind == "dir":
                result.dirs += 1
                subtree = row["subtree"]
                size = None if subtree is None else subtree + row["allocated_size"]
            else:
                result.others += 1
                size = row["size"]
            if not is_displayable(row["rel_path"]):
                result.not_displayable += 1
            yield Match(
                row["entry_id"],
                row["rel_path"],
                row["rel_path_display"],
                kind,
                size,
                row["mtime_ns"],
            )

    limit = max(limit, 0)
    if sort is Sort.SIZE:
        result.examples = heapq.nlargest(limit, matches(), key=lambda m: (m.size or 0, m.rel_path))
    elif sort is Sort.MTIME:
        result.examples = heapq.nlargest(limit, matches(), key=lambda m: m.mtime_ns)
    else:
        result.examples = heapq.nsmallest(limit, matches(), key=lambda m: m.rel_path)
    return result
