"""Summaries shown on the summary page, cached once their source is final."""

from __future__ import annotations

import sqlite3
from typing import Any

from treeaudit.db import cache
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.duplicates.groups import load_groups
from treeaudit.parasites.cached import parasite_findings
from treeaudit.parasites.detect import Band
from treeaudit.parasites.rules import rules_fingerprint
from treeaudit.scanner.models import ScanStatus

GROUPS_VERSION = "1"
"""Bump when the grouping or protection logic changes (invalidates cached summaries)."""


def _final(status: str) -> bool:
    return ScanStatus(status).is_final


def parasites_summary(conn: sqlite3.Connection, scan_id: int) -> dict[str, Any]:
    """Safe and to-check counts and sizes; cached for finished scans."""
    scan = scans_db.get_scan(conn, scan_id)
    key = f"parasites:{scan_id}:{rules_fingerprint()}"
    if scan is not None and _final(scan["status"]):
        cached = cache.get(conn, key)
        if cached is not None:
            return cached
    findings = parasite_findings(conn, scan_id)
    safe = [f for f in findings if f.band is Band.GREEN]
    check = [f for f in findings if f.band is not Band.GREEN]
    summary = {
        "safe_count": len(safe),
        "safe_size": sum(f.allocated_size or 0 for f in safe),
        "check_count": len(check),
        "check_size": sum(f.allocated_size or 0 for f in check),
    }
    if scan is not None and _final(scan["status"]):
        cache.put(conn, key, summary)
    return summary


def dupes_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Groups, removable copies and recoverable bytes of an analysis; cached when finished."""
    run = dupes_db.get_run(conn, run_id)
    key = f"dupes:{run_id}:{GROUPS_VERSION}"
    if run is not None and _final(run["status"]):
        cached = cache.get(conn, key)
        if cached is not None:
            return cached
    report = load_groups(conn, run_id)
    summary = {
        "groups": len(report.groups),
        "removable": sum(len(g.removable) for g in report.groups),
        "recoverable": sum(g.recoverable for g in report.groups),
    }
    if run is not None and _final(run["status"]):
        cache.put(conn, key, summary)
    return summary
