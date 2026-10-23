"""What the local AI is told about a folder to clean (D-040): metadata from the
index only, never the content of a file, nothing read on the disk."""

from __future__ import annotations

import sqlite3

from treeaudit.llm.opinion import MAX_NAMES, FolderFacts
from treeaudit.parasites.detect import Finding
from treeaudit.parasites.report import human_size

MAX_SUBTREE = 20_000
"""Entries looked at below the folder to count its extensions."""

# Forced join order and index, as in organize/plan.py: walking down the
# parent links, never reading the whole analysis at each level.
_EXTENSIONS_SQL = (
    "WITH RECURSIVE sub(id) AS ("
    "  SELECT ? UNION ALL"
    "  SELECT e.entry_id FROM sub CROSS JOIN entry e INDEXED BY entry_by_parent"
    "  ON e.scan_id = ? AND e.parent_id = sub.id LIMIT ?"
    ") "
    "SELECT COALESCE(e.extension, '') AS ext, COUNT(*) AS n "
    "FROM sub CROSS JOIN entry e ON e.scan_id = ? AND e.entry_id = sub.id "
    "WHERE e.kind = 'file' GROUP BY ext ORDER BY n DESC LIMIT 8"
)


def _names(conn: sqlite3.Connection, scan_id: int, parent: int | None, skip: int) -> list[str]:
    if parent is None:
        return []
    rows = conn.execute(
        "SELECT name_display FROM entry WHERE scan_id = ? AND parent_id = ? AND entry_id != ? "
        "ORDER BY size DESC, name_display LIMIT ?",
        (scan_id, parent, skip, MAX_NAMES),
    )
    return [row[0] for row in rows]


def facts_for_finding(conn: sqlite3.Connection, scan_id: int, finding: Finding) -> FolderFacts:
    row = conn.execute(
        "SELECT parent_id FROM entry WHERE scan_id = ? AND entry_id = ?",
        (scan_id, finding.entry_id),
    ).fetchone()
    parent = None if row is None else row[0]
    extensions = [
        (f".{ext}" if ext else "(sans extension)", n)
        for ext, n in conn.execute(
            _EXTENSIONS_SQL, (finding.entry_id, scan_id, MAX_SUBTREE, scan_id)
        )
    ]
    return FolderFacts(
        rule=finding.rule.label,
        rule_explanation=finding.rule.explanation,
        path=finding.rel_path_display,
        rule_confidence=finding.confidence,
        clues=[(result.evidence.label, result.matched) for result in finding.evidence],
        files=finding.files,
        size=None if finding.allocated_size is None else human_size(finding.allocated_size),
        extensions=extensions,
        children=_names(conn, scan_id, finding.entry_id, -1),
        siblings=_names(conn, scan_id, parent, finding.entry_id),
    )
