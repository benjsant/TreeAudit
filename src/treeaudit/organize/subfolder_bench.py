"""Scoring of sub-folder suggestions on known groups (D-040, D-049, D-050).

A pair of elements from the same expected group should share a suggested
sub-folder; a pair from different groups should not. The score is the share
of pairs handled right. A timid method that classifies almost nothing keeps
pairs apart for free: the coverage (elements of a real group that get a
sub-folder) and the generic names proposed are reported too. Used for the
fixed rules and for the local AI alike.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from treeaudit.organize.plan import Move, clean_folder_name
from treeaudit.organize.subfolder_cases import SubfolderCase
from treeaudit.organize.subfolders import Suggestions, is_generic_name, propose


@dataclass(frozen=True, slots=True)
class BenchResult:
    case: SubfolderCase
    suggestions: Suggestions | None
    error: str | None
    together: tuple[int, int]
    """(pairs of a group that share a sub-folder, pairs of a group)."""
    apart: tuple[int, int]
    """(pairs of different groups kept apart, pairs of different groups)."""
    names_valid: bool
    covered: tuple[int, int] = (0, 0)
    """(elements of a real group given a sub-folder, elements of a real group)."""
    generic: tuple[str, ...] = ()
    """Proposed names that say nothing of the content (D-049)."""

    @property
    def coverage(self) -> float:
        return self.covered[0] / self.covered[1] if self.covered[1] else 0.0

    @property
    def score(self) -> float:
        done = self.together[0] + self.apart[0]
        total = self.together[1] + self.apart[1]
        return done / total if total else 0.0


def case_moves(case: SubfolderCase) -> tuple[list[Move], dict[int, int]]:
    """The case as a plan's moves, and entry id -> index of its expected group."""
    moves: list[Move] = []
    group_of: dict[int, int] = {}
    for group_index, group in enumerate(case.groups):
        for name in group:
            entry = len(moves) + 1
            moves.append(
                Move(
                    entry_id=entry,
                    source=f"Bazar/{name}",
                    target=f"Rangé/{case.category}/{name}",
                    kind="file",
                    category=case.category,
                    size=50_000,
                    files=1,
                    mtime_s=1_580_000_000,
                    reason="fichier rangé par type",
                )
            )
            group_of[entry] = group_index
    return moves, group_of


def score(case: SubfolderCase, group_of: dict[int, int], found: Suggestions) -> BenchResult:
    together = apart = together_total = apart_total = 0
    for first, second in combinations(group_of, 2):
        a, b = found.by_entry.get(first), found.by_entry.get(second)
        if group_of[first] == group_of[second]:
            together_total += 1
            together += a is not None and a == b
        else:
            apart_total += 1
            apart += a is None or b is None or a != b
    valid = all(clean_folder_name(name) == name for name in found.subfolders)
    grouped = [e for e in group_of if len(case.groups[group_of[e]]) > 1]
    covered = (sum(e in found.by_entry for e in grouped), len(grouped))
    generic = tuple(n for n in found.subfolders if is_generic_name(n))
    return BenchResult(
        case,
        found,
        None,
        (together, together_total),
        (apart, apart_total),
        valid,
        covered,
        generic,
    )


def run_rules_bench(cases: Sequence[SubfolderCase]) -> list[BenchResult]:
    results = []
    for case in cases:
        moves, group_of = case_moves(case)
        results.append(score(case, group_of, propose(case.category, moves)))
    return results
