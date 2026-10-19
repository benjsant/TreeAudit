"""Running a scan: validation, walk, batched writes and final status (D-012)."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ScanLock, prepare_data_dir
from treeaudit.db import scans
from treeaudit.db.connection import connect
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext, validate_root
from treeaudit.scanner.models import (
    DirDone,
    EntryKind,
    EntryRecord,
    ErrorRecord,
    ExclusionRecord,
    ScanStatus,
    Totals,
)
from treeaudit.scanner.walker import DEFAULT_MAX_DEPTH, WalkOptions, parse_subpath, walk

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanRequest:
    root: RootConfig
    subpath: str = ""
    max_depth: int = DEFAULT_MAX_DEPTH


@dataclass(slots=True)
class ScanProgress:
    """Written by the scanning thread only; other threads just read it."""

    scan_id: int | None = None
    entries: int = 0
    errors: int = 0
    exclusions: int = 0
    current_dir: str = ""


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    scan_id: int
    status: ScanStatus
    entries: int
    errors: int
    exclusions: int
    totals: Totals = field(default_factory=Totals)
    failure: str | None = None


def mount_points_below(mounts: MountTable, root_path: str) -> frozenset[str]:
    prefix = root_path.rstrip("/") + "/"
    return frozenset(e.mount_point for e in mounts.entries if e.mount_point.startswith(prefix))


def compute_totals(conn: sqlite3.Connection, scan_id: int) -> Totals:
    """Totals of what was actually recorded, each inode counted once (D-022).

    Used for every final status, so a cancelled or incomplete scan reports what it
    saw, never more.
    """
    counts = conn.execute(
        "SELECT "
        "COALESCE(SUM(kind = 'file'), 0), COALESCE(SUM(kind = 'dir' AND depth > 0), 0), "
        "COALESCE(SUM(kind = 'symlink'), 0), COALESCE(SUM(kind = 'other'), 0) "
        "FROM entry WHERE scan_id = ?",
        (scan_id,),
    ).fetchone()
    logical = conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM (SELECT MAX(size) AS size FROM entry "
        "WHERE scan_id = ? AND kind = 'file' GROUP BY dev, ino)",
        (scan_id,),
    ).fetchone()[0]
    allocated = conn.execute(
        "SELECT COALESCE(SUM(allocated_size), 0) FROM (SELECT MAX(allocated_size) "
        "AS allocated_size FROM entry WHERE scan_id = ? GROUP BY dev, ino)",
        (scan_id,),
    ).fetchone()[0]
    return Totals(
        files=counts[0],
        dirs=counts[1],
        symlinks=counts[2],
        others=counts[3],
        logical_size=logical,
        allocated_size=allocated,
    )


def run_scan(
    conn: sqlite3.Connection,
    request: ScanRequest,
    *,
    ctx: ValidationContext,
    cancel: threading.Event,
    progress: ScanProgress | None = None,
    batch_size: int = scans.DEFAULT_BATCH_SIZE,
) -> ScanOutcome:
    """Scan one root (or a sub-folder of it) and record the result.

    Raises RootRejectedError or SubpathError before creating any scan record if
    the request is not acceptable.
    """
    root = request.root
    subpath = parse_subpath(request.subpath)
    # Revalidate right before scanning: the root may have changed since startup.
    validate_root(root.path, ctx, allow_system_volume=root.allow_system_volume)
    root_stat = os.lstat(root.path)
    options = WalkOptions(
        root_path=root.path,
        mount_points=mount_points_below(ctx.mounts, root.path),
        cross_mounts=root.cross_mounts,
        max_depth=request.max_depth,
        expected_root=(root_stat.st_dev, root_stat.st_ino),
    )
    scan_id = scans.create_scan(
        conn,
        root_id=root.id,
        root_path=root.path,
        subpath="/".join(subpath),
        options={"cross_mounts": root.cross_mounts, "max_depth": request.max_depth},
    )
    progress = progress if progress is not None else ScanProgress()
    progress.scan_id = scan_id
    writer = scans.ScanWriter(conn, scan_id, batch_size)
    entries = errors = exclusions = 0
    cancelled = False
    events = walk(options, subpath)
    try:
        for event in events:
            if isinstance(event, EntryRecord):
                writer.add_entry(event)
                entries += 1
                progress.entries = entries
                if event.kind is EntryKind.DIR:
                    progress.current_dir = scans.display(event.rel_path)[0]
            elif isinstance(event, ErrorRecord):
                writer.add_error(event)
                errors += 1
                progress.errors = errors
            elif isinstance(event, ExclusionRecord):
                writer.add_exclusion(event)
                exclusions += 1
                progress.exclusions = exclusions
            elif isinstance(event, DirDone):
                writer.add_dir_summary(event.entry_id, event.totals)
            if cancel.is_set():
                cancelled = True
                break
        events.close()  # releases every open directory descriptor
        writer.flush()
    except Exception as exc:
        events.close()
        logger.exception("scan %d failed", scan_id)
        failure = f"{type(exc).__name__}: {exc}"
        scans.finish_scan(
            conn,
            scan_id,
            status=ScanStatus.FAILED,
            entries=entries,
            totals=Totals(),
            errors=errors,
            exclusions=exclusions,
            failure=failure,
        )
        return ScanOutcome(scan_id, ScanStatus.FAILED, entries, errors, exclusions, failure=failure)

    if cancelled:
        status = ScanStatus.CANCELLED
    elif errors:
        status = ScanStatus.INCOMPLETE
    else:
        status = ScanStatus.COMPLETE
    totals = compute_totals(conn, scan_id)
    totals.errors = errors
    scans.finish_scan(
        conn,
        scan_id,
        status=status,
        entries=entries,
        totals=totals,
        errors=errors,
        exclusions=exclusions,
    )
    return ScanOutcome(scan_id, status, entries, errors, exclusions, totals)


def execute_scan(
    config: AppConfig,
    request: ScanRequest,
    *,
    mounts: MountTable,
    home: str | None,
    cancel: threading.Event,
    progress: ScanProgress | None = None,
) -> ScanOutcome:
    """Prepare the data directory, take the scan lock and run the scan."""
    prepare_data_dir(config.data_dir)
    ctx = ValidationContext(
        mounts=mounts,
        home=home,
        data_dir=config.data_dir,
        check_filesystem=True,
        require_read_only_mount=config.container_mode,
    )
    with ScanLock(config.data_dir):
        conn = connect(config.data_dir)
        try:
            recovered = scans.fail_interrupted_scans(conn)
            if recovered:
                logger.warning("%d interrupted scan(s) marked as failed", recovered)
            return run_scan(conn, request, ctx=ctx, cancel=cancel, progress=progress)
        finally:
            conn.close()
