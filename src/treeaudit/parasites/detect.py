"""Detection of parasite entries in a recorded scan (D-023).

Works on the SQLite index only: the audited disk is never read again. Each
finding carries the evidence that produced its confidence, so the result can
always be explained.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from treeaudit.parasites.rules import Evidence, EvidenceKind, Recreate, Rule, Target

GREEN_THRESHOLD = 80
ORANGE_THRESHOLD = 50
DEFAULT_MIN_CONFIDENCE = 30

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# SQL in this module is assembled from constant fragments (column names, fixed
# conditions) only; every value, including rule names and globs, is bound with "?".

_ENTRY_COLUMNS = (
    "entry_id, parent_id, depth, name, name_display, rel_path, rel_path_display, allocated_size"
)


class Band(StrEnum):
    GREEN = "green"  # rebuildable, deletion very likely safe
    ORANGE = "orange"  # probable, check first
    RED = "red"  # doubtful, do not delete without looking


def band_of(confidence: int) -> Band:
    if confidence >= GREEN_THRESHOLD:
        return Band.GREEN
    if confidence >= ORANGE_THRESHOLD:
        return Band.ORANGE
    return Band.RED


_BAND_ORDER = {Band.GREEN: 0, Band.ORANGE: 1, Band.RED: 2}


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    evidence: Evidence
    matched: bool


@dataclass(frozen=True, slots=True)
class Finding:
    rule: Rule
    entry_id: int
    depth: int
    name: bytes
    name_display: str
    rel_path: bytes
    rel_path_display: str
    confidence: int
    evidence: tuple[EvidenceResult, ...]
    allocated_size: int | None
    """Space used by the target, or None if its subtree was not fully scanned."""
    files: int | None
    recreate: Recreate | None
    number: int = 0

    @property
    def band(self) -> Band:
        return band_of(self.confidence)

    @property
    def displayable(self) -> bool:
        """True if the path can be shown and quoted safely in a shell command."""
        return is_displayable(self.rel_path)


def is_displayable(raw: bytes) -> bool:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not _CONTROL_CHARS.search(text)


def _exists(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> bool:
    return bool(conn.execute(f"SELECT EXISTS ({sql})", params).fetchone()[0])


class _Evaluator:
    def __init__(self, conn: sqlite3.Connection, scan_id: int) -> None:
        self.conn = conn
        self.scan_id = scan_id

    def candidates(self, rule: Rule) -> Iterable[sqlite3.Row]:
        kind = "dir" if rule.target is Target.DIR else "file"
        seen: set[int] = set()
        base = f"SELECT {_ENTRY_COLUMNS} FROM entry WHERE scan_id = ? AND kind = ? AND depth > 0"  # noqa: S608
        if rule.names:
            placeholders = ", ".join("?" * len(rule.names))
            names = [name.encode() for name in rule.names]
            for row in self.conn.execute(
                f"{base} AND name IN ({placeholders})", (self.scan_id, kind, *names)
            ):
                seen.add(row["entry_id"])
                yield row
        for glob in rule.globs:
            for row in self.conn.execute(
                f"{base} AND name_valid_utf8 = 1 AND name_display GLOB ?",
                (self.scan_id, kind, glob),
            ):
                # SQLite GLOB and fnmatch agree on the restricted syntax; confirm anyway.
                if row["entry_id"] not in seen and fnmatch.fnmatchcase(row["name_display"], glob):
                    seen.add(row["entry_id"])
                    yield row

    def _child(self, parent_id: int, value: str, glob: bool, exclude: int | None = None) -> bool:
        column, operand = ("name_display", "GLOB ?") if glob else ("name", "= ?")
        param: object = value if glob else value.encode()
        sql = f"SELECT 1 FROM entry WHERE scan_id = ? AND parent_id = ? AND {column} {operand}"  # noqa: S608
        params: list[object] = [self.scan_id, parent_id, param]
        if exclude is not None:
            sql += " AND entry_id != ?"
            params.append(exclude)
        return _exists(self.conn, sql, params)

    def _subtree(self, rel_path: bytes) -> tuple[str, list[object]]:
        # Every descendant path starts with "<rel_path>/"; "0" is the byte after "/".
        return (
            "scan_id = ? AND kind = 'file' AND rel_path > ? AND rel_path < ?",
            [self.scan_id, rel_path + b"/", rel_path + b"0"],
        )

    def check(self, evidence: Evidence, row: sqlite3.Row) -> bool:
        kind = evidence.kind
        if kind in (EvidenceKind.CHILD, EvidenceKind.CHILD_GLOB):
            return self._child(row["entry_id"], evidence.value, kind.is_glob)
        if kind in (EvidenceKind.SIBLING, EvidenceKind.SIBLING_GLOB):
            return self._child(
                row["parent_id"], evidence.value, kind.is_glob, exclude=row["entry_id"]
            )
        where, params = self._subtree(row["rel_path"])
        if kind is EvidenceKind.ANY_FILE_GLOB:
            return _exists(
                self.conn,
                f"SELECT 1 FROM entry WHERE {where} AND name_display GLOB ?",  # noqa: S608
                [*params, evidence.value],
            )
        if kind is EvidenceKind.ANY_FILE_NOT_GLOB:
            return _exists(
                self.conn,
                f"SELECT 1 FROM entry WHERE {where} AND NOT (name_display GLOB ?)",  # noqa: S608
                [*params, evidence.value],
            )
        total, matching = self.conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(name_display GLOB ?), 0) FROM entry WHERE {where}",  # noqa: S608
            [evidence.value, *params],
        ).fetchone()
        return bool(total) and total == matching

    def size(self, rule: Rule, row: sqlite3.Row) -> tuple[int | None, int | None]:
        if rule.target is Target.FILE:
            return row["allocated_size"], None
        summary = self.conn.execute(
            "SELECT allocated_size, files FROM dir_summary WHERE scan_id = ? AND entry_id = ?",
            (self.scan_id, row["entry_id"]),
        ).fetchone()
        if summary is None:  # subtree not fully scanned (error, exclusion, cancellation)
            return None, None
        return summary["allocated_size"] + row["allocated_size"], summary["files"]

    def recreate(self, rule: Rule, row: sqlite3.Row) -> Recreate | None:
        for option in rule.recreate:
            if option.when_sibling is None or self._child(
                row["parent_id"], option.when_sibling, glob=False, exclude=row["entry_id"]
            ):
                return option
        return None

    def evaluate(self, rule: Rule, row: sqlite3.Row) -> Finding:
        results = tuple(EvidenceResult(e, self.check(e, row)) for e in rule.evidence)
        score = rule.base + sum(r.evidence.weight for r in results if r.matched)
        size, files = self.size(rule, row)
        return Finding(
            rule=rule,
            entry_id=row["entry_id"],
            depth=row["depth"],
            name=row["name"],
            name_display=row["name_display"],
            rel_path=row["rel_path"],
            rel_path_display=row["rel_path_display"],
            confidence=max(0, min(score, rule.max_confidence, 100)),
            evidence=results,
            allocated_size=size,
            files=files,
            recreate=self.recreate(rule, row),
        )


def _has_ancestor_in(rel_path: bytes, ancestors: set[bytes]) -> bool:
    parts = rel_path.split(b"/")
    return any(b"/".join(parts[:i]) in ancestors for i in range(1, len(parts)))


def find_parasites(
    conn: sqlite3.Connection,
    scan_id: int,
    rules: Sequence[Rule],
    *,
    min_confidence: int = DEFAULT_MIN_CONFIDENCE,
) -> list[Finding]:
    """Evaluate every rule on a scan and return numbered findings, best first.

    When several rules match the same entry, the most confident one is kept.
    Entries inside a green finding are not reported again (deleting the
    ancestor already covers them).
    """
    evaluator = _Evaluator(conn, scan_id)
    best: dict[int, Finding] = {}
    for rule in rules:
        for row in evaluator.candidates(rule):
            finding = evaluator.evaluate(rule, row)
            current = best.get(finding.entry_id)
            if current is None or finding.confidence > current.confidence:
                best[finding.entry_id] = finding

    kept: list[Finding] = []
    green_dirs: set[bytes] = set()
    for finding in sorted(best.values(), key=lambda f: (f.depth, f.rel_path)):
        if _has_ancestor_in(finding.rel_path, green_dirs):
            continue
        if finding.confidence < min_confidence:
            continue
        kept.append(finding)
        if finding.band is Band.GREEN and finding.rule.target is Target.DIR:
            green_dirs.add(finding.rel_path)

    kept.sort(key=lambda f: (_BAND_ORDER[f.band], -(f.allocated_size or 0), f.rel_path))
    return [dataclasses.replace(f, number=index) for index, f in enumerate(kept, start=1)]
