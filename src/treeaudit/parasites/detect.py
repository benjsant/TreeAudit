"""Detection of parasite entries in a recorded scan (D-023).

Works on the SQLite index only: the audited disk is never read again. Each
finding carries the evidence that produced its confidence, so the result can
always be explained.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from treeaudit.parasites.rules import Evidence, EvidenceKind, Recreate, Rule, Target
from treeaudit.shellcmd import is_displayable

GREEN_THRESHOLD = 80
ORANGE_THRESHOLD = 50
DEFAULT_MIN_CONFIDENCE = 30

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


def _exists(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> bool:
    return bool(conn.execute(f"SELECT EXISTS ({sql})", params).fetchone()[0])


class _Evaluator:
    def __init__(self, conn: sqlite3.Connection, scan_id: int, rules: Sequence[Rule]) -> None:
        self.conn = conn
        self.scan_id = scan_id
        self.rules = rules
        self._glob_cache: dict[str, list[sqlite3.Row]] = {}

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
            for row in self._glob_rows(kind):
                # SQLite GLOB and fnmatch agree on the restricted syntax; confirm anyway.
                if row["entry_id"] not in seen and fnmatch.fnmatchcase(row["name_display"], glob):
                    seen.add(row["entry_id"])
                    yield row

    def _glob_rows(self, kind: str) -> list[sqlite3.Row]:
        """Entries matching any glob of any rule for this kind, in ONE pass over the scan.

        Globs with a leading wildcard cannot use an index: one query per glob meant
        about twenty full scans of the table (5 s on 1 000 000 entries).
        """
        if kind not in self._glob_cache:
            globs = sorted({g for r in self.rules if self._kind(r) == kind for g in r.globs})
            if not globs:
                self._glob_cache[kind] = []
            else:
                any_glob = " OR ".join("name_display GLOB ?" for _ in globs)
                self._glob_cache[kind] = self.conn.execute(
                    f"SELECT {_ENTRY_COLUMNS} FROM entry WHERE scan_id = ? AND kind = ? "  # noqa: S608
                    f"AND depth > 0 AND name_valid_utf8 = 1 AND ({any_glob})",
                    (self.scan_id, kind, *globs),
                ).fetchall()
        return self._glob_cache[kind]

    @staticmethod
    def _kind(rule: Rule) -> str:
        return "dir" if rule.target is Target.DIR else "file"

    def _child(self, parent_id: int, value: str, glob: bool, exclude: int | None = None) -> bool:
        column, operand = ("name_display", "GLOB ?") if glob else ("name", "= ?")
        param: object = value if glob else value.encode()
        sql = f"SELECT 1 FROM entry WHERE scan_id = ? AND parent_id = ? AND {column} {operand}"  # noqa: S608
        params: list[object] = [self.scan_id, parent_id, param]
        if exclude is not None:
            sql += " AND entry_id != ?"
            params.append(exclude)
        return _exists(self.conn, sql, params)

    def _subtree_query(
        self, entry_id: int, columns: str, column_params: list[object], condition: str
    ) -> tuple[str, list[object]]:
        """Files below an entry, found through the parent links (index entry_by_parent).

        Only evaluated for the few candidates whose rule looks inside the folder,
        so a path index (a fifth of the database) is not worth its cost. Parameters
        are listed in the order of their "?" in the SQL text.
        """
        sql = (
            "WITH RECURSIVE sub(id) AS ("  # noqa: S608 - constant fragments only, values bound
            "SELECT entry_id FROM entry WHERE scan_id = ? AND parent_id = ? "
            "UNION ALL SELECT e.entry_id FROM entry e JOIN sub "
            "ON e.scan_id = ? AND e.parent_id = sub.id) "
            f"SELECT {columns} FROM entry WHERE scan_id = ? AND kind = 'file' "
            f"AND entry_id IN sub {condition}"
        )
        params = [self.scan_id, entry_id, self.scan_id, *column_params, self.scan_id]
        return sql, params

    def check(self, evidence: Evidence, row: sqlite3.Row) -> bool:
        kind = evidence.kind
        if kind in (EvidenceKind.CHILD, EvidenceKind.CHILD_GLOB):
            return self._child(row["entry_id"], evidence.value, kind.is_glob)
        if kind in (EvidenceKind.SIBLING, EvidenceKind.SIBLING_GLOB):
            return self._child(
                row["parent_id"], evidence.value, kind.is_glob, exclude=row["entry_id"]
            )
        if kind in (EvidenceKind.ANY_FILE_GLOB, EvidenceKind.ANY_FILE_NOT_GLOB):
            negate = "NOT " if kind is EvidenceKind.ANY_FILE_NOT_GLOB else ""
            sql, params = self._subtree_query(
                row["entry_id"], "1", [], f"AND {negate}(name_display GLOB ?)"
            )
            return _exists(self.conn, sql, [*params, evidence.value])
        sql, params = self._subtree_query(
            row["entry_id"], "COUNT(*), COALESCE(SUM(name_display GLOB ?), 0)", [evidence.value], ""
        )
        total, matching = self.conn.execute(sql, params).fetchone()
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


def evaluate_candidates(
    conn: sqlite3.Connection, scan_id: int, rules: Sequence[Rule]
) -> list[Finding]:
    """Evaluate every rule: the best (most confident) finding per entry, unnumbered.

    This is the costly part (queries on the index); select_findings() then
    filters, removes entries inside green folders, sorts and numbers.
    """
    evaluator = _Evaluator(conn, scan_id, rules)
    best: dict[int, Finding] = {}
    for rule in rules:
        for row in evaluator.candidates(rule):
            finding = evaluator.evaluate(rule, row)
            current = best.get(finding.entry_id)
            if current is None or finding.confidence > current.confidence:
                best[finding.entry_id] = finding
    return list(best.values())


def select_findings(
    candidates: Sequence[Finding], *, min_confidence: int = DEFAULT_MIN_CONFIDENCE
) -> list[Finding]:
    """Numbered findings, best first. Entries inside a green finding are not
    reported again (deleting the ancestor already covers them)."""
    kept: list[Finding] = []
    green_dirs: set[bytes] = set()
    for finding in sorted(candidates, key=lambda f: (f.depth, f.rel_path)):
        if _has_ancestor_in(finding.rel_path, green_dirs):
            continue
        if finding.confidence < min_confidence:
            continue
        kept.append(finding)
        if finding.band is Band.GREEN and finding.rule.target is Target.DIR:
            green_dirs.add(finding.rel_path)

    kept.sort(key=lambda f: (_BAND_ORDER[f.band], -(f.allocated_size or 0), f.rel_path))
    return [dataclasses.replace(f, number=index) for index, f in enumerate(kept, start=1)]


def find_parasites(
    conn: sqlite3.Connection,
    scan_id: int,
    rules: Sequence[Rule],
    *,
    min_confidence: int = DEFAULT_MIN_CONFIDENCE,
) -> list[Finding]:
    """Evaluate every rule on a scan and return numbered findings, best first.

    When several rules match the same entry, the most confident one is kept.
    """
    return select_findings(evaluate_candidates(conn, scan_id, rules), min_confidence=min_confidence)
