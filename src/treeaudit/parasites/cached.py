"""Parasite findings of a finished scan, evaluated once and kept in the index cache.

Only the costly evaluation of the rules is cached; the selection (confidence
threshold, entries inside green folders, order, numbers) is redone each time
with select_findings(), so the numbers are exactly those of find_parasites().
The cache key includes the fingerprint of the rules catalogue.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from treeaudit.db import cache
from treeaudit.db import scans as scans_db
from treeaudit.parasites.detect import (
    DEFAULT_MIN_CONFIDENCE,
    EvidenceResult,
    Finding,
    evaluate_candidates,
    select_findings,
)
from treeaudit.parasites.rules import Rule, load_default_rules, rules_fingerprint
from treeaudit.scanner.models import ScanStatus


def _encode(finding: Finding) -> dict[str, Any]:
    rule = finding.rule
    return {
        "rule": rule.id,
        "entry_id": finding.entry_id,
        "depth": finding.depth,
        "name": finding.name.hex(),
        "name_display": finding.name_display,
        "rel_path": finding.rel_path.hex(),
        "rel_path_display": finding.rel_path_display,
        "confidence": finding.confidence,
        "matched": [result.matched for result in finding.evidence],
        "allocated_size": finding.allocated_size,
        "files": finding.files,
        "recreate": None if finding.recreate is None else rule.recreate.index(finding.recreate),
    }


def _decode(data: dict[str, Any], rules: dict[str, Rule]) -> Finding:
    rule = rules[data["rule"]]
    recreate_index = data["recreate"]
    return Finding(
        rule=rule,
        entry_id=data["entry_id"],
        depth=data["depth"],
        name=bytes.fromhex(data["name"]),
        name_display=data["name_display"],
        rel_path=bytes.fromhex(data["rel_path"]),
        rel_path_display=data["rel_path_display"],
        confidence=data["confidence"],
        evidence=tuple(
            EvidenceResult(evidence, matched)
            for evidence, matched in zip(rule.evidence, data["matched"], strict=True)
        ),
        allocated_size=data["allocated_size"],
        files=data["files"],
        recreate=None if recreate_index is None else rule.recreate[recreate_index],
    )


def parasite_findings(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    min_confidence: int = DEFAULT_MIN_CONFIDENCE,
) -> list[Finding]:
    """Same result as find_parasites() with the shipped rules, cached for finished scans."""
    rules = load_default_rules()
    scan = scans_db.get_scan(conn, scan_id)
    final = scan is not None and ScanStatus(scan["status"]).is_final
    key = f"parasite-candidates:{scan_id}:{rules_fingerprint()}"
    cached = cache.get(conn, key) if final else None
    if cached is not None:
        by_id = {rule.id: rule for rule in rules}
        candidates = [_decode(item, by_id) for item in cached["candidates"]]
    else:
        candidates = evaluate_candidates(conn, scan_id, rules)
        if final:
            cache.put(conn, key, {"candidates": [_encode(f) for f in candidates]})
    return select_findings(candidates, min_confidence=min_confidence)
