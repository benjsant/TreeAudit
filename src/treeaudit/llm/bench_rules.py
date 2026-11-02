"""What the rules alone say on the benchmark's cases (D-052).

Each case describes a folder as the AI sees it. Here it is rebuilt as a real
temporary tree (the folder, its files and sub-folders, its neighbours), then
scanned and given to the real rules, without any AI. The result is what the
user would see before asking the AI: the colour and the confidence of the
rule. It tells whether the AI's opinion adds anything to them.

The rebuilt trees are approximate: at most MAX_FILES_PER_EXTENSION files per
extension, a few bytes each. Nothing outside a fresh temporary folder is
written, and the folder is removed afterwards.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from treeaudit.db import scans
from treeaudit.db.connection import connect_memory
from treeaudit.llm.bench import Case
from treeaudit.llm.client import LlmClient, LlmError
from treeaudit.llm.opinion import ask_opinion
from treeaudit.parasites.detect import Band, find_parasites
from treeaudit.parasites.rules import load_default_rules
from treeaudit.scanner.models import (
    DirDone,
    EntryRecord,
    ErrorRecord,
    ExclusionRecord,
    ScanStatus,
)
from treeaudit.scanner.runner import compute_totals
from treeaudit.scanner.walker import WalkOptions, walk

MAX_FILES_PER_EXTENSION = 30
NO_EXTENSION = "(sans extension)"


@dataclass(frozen=True, slots=True)
class RuleView:
    """What the rules alone show for one case."""

    case: Case
    rule: str | None
    """The rule that matched the folder, or None if no rule did."""
    confidence: int | None
    band: Band | None

    @property
    def to_clean(self) -> bool:
        return "nettoyable" in self.case.expected

    @property
    def checked(self) -> bool:
        """Green: proposed for deletion, checked by default."""
        return self.band is Band.GREEN

    @property
    def shown(self) -> bool:
        """Listed at all (orange and red are listed, unchecked)."""
        return self.band is not None

    @property
    def dangerous(self) -> bool:
        """A folder to keep, checked by default: the costly mistake."""
        return self.checked and not self.to_clean


def _is_file(name: str) -> bool:
    stem, dot, extension = name.rpartition(".")
    return bool(dot and stem and extension) and not extension.isdigit()


def _make(parent: Path, name: str) -> None:
    path = parent / name
    if _is_file(name):
        path.write_bytes(b"x")
    else:
        path.mkdir(exist_ok=True)


def build_tree(case: Case, base: Path) -> Path:
    """Rebuild the case's folder under ``base``; return the folder's path."""
    folder = base.joinpath(*case.facts.path.split("/"))
    folder.mkdir(parents=True)
    for name in case.facts.siblings:
        _make(folder.parent, name)
    for name in case.facts.children:
        _make(folder, name)
    for extension, count in case.facts.extensions:
        suffix = "" if extension == NO_EXTENSION else extension
        for index in range(min(count, MAX_FILES_PER_EXTENSION)):
            (folder / f"fichier-{index}{suffix}").write_bytes(b"x")
    return folder


def _scan(root: Path) -> tuple[sqlite3.Connection, int]:
    conn = connect_memory()
    scan_id = scans.create_scan(conn, root_id="bench", root_path=str(root), subpath="", options={})
    writer = scans.ScanWriter(conn, scan_id)
    entries = 0
    for event in walk(WalkOptions(root_path=str(root))):
        if isinstance(event, EntryRecord):
            writer.add_entry(event)
            entries += 1
        elif isinstance(event, ErrorRecord):
            writer.add_error(event)
        elif isinstance(event, ExclusionRecord):
            writer.add_exclusion(event)
        elif isinstance(event, DirDone):
            writer.add_dir_summary(event.entry_id, event.totals)
    writer.flush()
    totals = compute_totals(conn, scan_id)
    scans.finish_scan(
        conn,
        scan_id,
        status=ScanStatus.COMPLETE,
        entries=entries,
        totals=totals,
        errors=0,
        exclusions=0,
    )
    return conn, scan_id


def rule_view(case: Case) -> RuleView:
    """Rebuild the case, scan it, and read what the real rules say of it."""
    with tempfile.TemporaryDirectory(prefix="treeaudit-bench-") as tmp:
        root = Path(tmp) / "racine"
        build_tree(case, root)
        conn, scan_id = _scan(root)
        try:
            findings = find_parasites(conn, scan_id, load_default_rules(), min_confidence=0)
        finally:
            conn.close()
    target = os.fsencode(case.facts.path)
    for finding in findings:
        if finding.rel_path == target:
            return RuleView(case, finding.rule.label, finding.confidence, finding.band)
    return RuleView(case, None, None, None)


def run_rules_view(cases: tuple[Case, ...]) -> list[RuleView]:
    return [rule_view(case) for case in cases]


@dataclass(frozen=True, slots=True)
class Compared:
    """The rules' view of a case and, where the page would offer it, the AI's."""

    view: RuleView
    verdict: str | None = None
    """The AI's verdict, after guard(); None when not asked or on error."""
    asked: bool = False

    @property
    def alert(self) -> bool:
        """A useful alarm: "keep" on a folder to keep."""
        return self.verdict == "a_garder" and not self.view.to_clean

    @property
    def helps(self) -> bool:
        """ "Nothing to keep" on a folder that is indeed to clean."""
        return self.verdict == "nettoyable" and self.view.to_clean

    @property
    def dangerous(self) -> bool:
        """ "Nothing to keep" on a folder to keep."""
        return self.verdict == "nettoyable" and not self.view.to_clean


def compare(views: list[RuleView], client: LlmClient, *, pause: float = 0.0) -> list[Compared]:
    """Ask the AI where the page offers it: listed, not green (D-040).

    The AI sees the case's facts with the real rule's name and confidence,
    as the page would give them (llm/facts.py). ``pause`` seconds between two
    opinions let the graphics card rest instead of running flat out."""
    results = []
    asked = 0
    for view in views:
        if not view.shown or view.checked:
            results.append(Compared(view))
            continue
        if asked and pause > 0:
            time.sleep(pause)
        asked += 1
        facts = dataclasses.replace(
            view.case.facts, rule=view.rule or "", rule_confidence=view.confidence or 0
        )
        try:
            opinion = ask_opinion(client, facts)[0]
        except LlmError:
            results.append(Compared(view, None, asked=True))
        else:
            results.append(Compared(view, opinion.verdict, asked=True))
    return results
