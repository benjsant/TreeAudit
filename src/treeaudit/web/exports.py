"""Local exports (JSON and CSV) of scans, parasite findings, duplicates and searches.

CSV cells are protected against formula injection: a file name such as
``=HYPERLINK(...)`` or ``+cmd|...`` would otherwise run as a formula when the
file is opened in a spreadsheet. Such cells are prefixed with an apostrophe.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from treeaudit import __version__
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.db.connection import connect
from treeaudit.duplicates.groups import load_groups
from treeaudit.filters.search import Sort, search
from treeaudit.filters.spec import FilterError, compile_filter, describe
from treeaudit.parasites.detect import find_parasites
from treeaudit.parasites.rules import load_default_rules
from treeaudit.web.api import Config, Db
from treeaudit.web.search_form import SearchForm, spec_from_form

router = APIRouter(prefix="/export")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_SEARCH_EXPORT = 100_000


def csv_cell(value: Any) -> Any:
    """Neutralise values a spreadsheet would interpret as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


CSV_BATCH = 2000


def _csv_lines(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> Iterator[str]:
    """CSV text in batches: one chunk per row made streaming 150 000 rows take 25 s."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    pending = 0
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
        pending += 1
        if pending >= CSV_BATCH:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
            pending = 0
    yield buffer.getvalue()


def _download(filename: str) -> dict[str, str]:
    # filename is built from integers and fixed words only.
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def _csv(filename: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> Response:
    return StreamingResponse(
        _csv_lines(header, rows),
        media_type="text/csv; charset=utf-8",
        headers=_download(filename),
    )


def _json(filename: str, payload: dict[str, Any]) -> Response:
    payload = {"treeaudit_version": __version__, **payload}
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers=_download(filename),
    )


def _scan(conn: Any, scan_id: int) -> Any:
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "scan inconnu")
    return scan


@router.get("/scans/{scan_id}.json")
def scan_json(scan_id: int, conn: Db) -> Response:
    scan = _scan(conn, scan_id)
    return _json(
        f"treeaudit-scan{scan_id}.json",
        {
            "scan": {k: scan[k] for k in scan.keys() if k != "root_path"},  # noqa: SIM118
            "extensions": [
                {"extension": e.extension, "files": e.files, "logical_size": e.logical_size}
                for e in scans_db.top_extensions(conn, scan_id, limit=100)
            ],
            "largest_dirs": [
                {
                    "path": d.rel_path_display,
                    "files": d.files,
                    "dirs": d.dirs,
                    "allocated_size": d.allocated_size,
                }
                for d in scans_db.largest_dirs(conn, scan_id, limit=100)
            ],
            "errors": [dict(row) for row in scans_db.list_errors(conn, scan_id, limit=10_000)],
            "exclusions": [
                dict(row) for row in scans_db.list_exclusions(conn, scan_id, limit=10_000)
            ],
        },
    )


@router.get("/scans/{scan_id}/entries.csv")
def entries_csv(scan_id: int, conn: Db, config: Config) -> Response:
    _scan(conn, scan_id)

    def rows() -> Iterator[Sequence[Any]]:
        # Own connection: the request's one may be closed before streaming ends.
        stream = connect(config.data_dir)
        try:
            yield from stream.execute(
                "SELECT rel_path_display, kind, extension, size, allocated_size, mtime_ns "
                "FROM entry WHERE scan_id = ? ORDER BY entry_id",
                (scan_id,),
            )
        finally:
            stream.close()

    header = ["path", "kind", "extension", "size", "allocated_size", "mtime_ns"]
    return _csv(f"treeaudit-scan{scan_id}-entries.csv", header, rows())


def _parasites(conn: Any, scan_id: int, min_confidence: int) -> list[Any]:
    _scan(conn, scan_id)
    return find_parasites(conn, scan_id, load_default_rules(), min_confidence=min_confidence)


@router.get("/scans/{scan_id}/parasites.csv")
def parasites_csv(
    scan_id: int, conn: Db, min_confidence: Annotated[int, Query(ge=0, le=100)] = 30
) -> Response:
    findings = _parasites(conn, scan_id, min_confidence)
    rows = (
        (
            f.number,
            f.band.value,
            f.confidence,
            f.rule.id,
            f.rule.label,
            f.rel_path_display,
            f.allocated_size,
            "; ".join(r.evidence.label for r in f.evidence if r.matched),
        )
        for f in findings
    )
    header = ["number", "band", "confidence", "rule", "label", "path", "size", "evidence"]
    return _csv(f"treeaudit-scan{scan_id}-parasites.csv", header, rows)


@router.get("/scans/{scan_id}/parasites.json")
def parasites_json(
    scan_id: int, conn: Db, min_confidence: Annotated[int, Query(ge=0, le=100)] = 30
) -> Response:
    findings = _parasites(conn, scan_id, min_confidence)
    return _json(
        f"treeaudit-scan{scan_id}-parasites.json",
        {
            "scan_id": scan_id,
            "min_confidence": min_confidence,
            "findings": [
                {
                    "number": f.number,
                    "band": f.band.value,
                    "confidence": f.confidence,
                    "rule": f.rule.id,
                    "label": f.rule.label,
                    "path": f.rel_path_display,
                    "size": f.allocated_size,
                    "evidence": [
                        {"label": r.evidence.label, "weight": r.evidence.weight, "found": r.matched}
                        for r in f.evidence
                    ],
                }
                for f in findings
            ],
        },
    )


def _dupes(conn: Any, run_id: int) -> Any:
    if dupes_db.get_run(conn, run_id) is None:
        raise HTTPException(404, "analyse inconnue")
    return load_groups(conn, run_id)


@router.get("/dupes/{run_id}.csv")
def dupes_csv(run_id: int, conn: Db) -> Response:
    report = _dupes(conn, run_id)

    def rows() -> Iterator[Sequence[Any]]:
        for group in report.groups:
            kept = {c.entry_id for c in group.keep}
            for copy in group.copies:
                yield (
                    group.number,
                    group.size,
                    group.digest.hex() if group.digest else "",
                    copy.rel_path_display,
                    "keep" if copy.entry_id in kept else "remove",
                    copy.protection or "",
                )

    header = ["group", "size", "sha256", "path", "suggestion", "protection"]
    return _csv(f"treeaudit-dupes{run_id}.csv", header, rows())


@router.get("/dupes/{run_id}.json")
def dupes_json(run_id: int, conn: Db) -> Response:
    report = _dupes(conn, run_id)
    return _json(
        f"treeaudit-dupes{run_id}.json",
        {
            "run_id": run_id,
            "micro": report.micro,
            "protected_copies": report.protected_copies,
            "hardlinked_inodes": report.hardlinked_inodes,
            "groups": [
                {
                    "number": g.number,
                    "size": g.size,
                    "sha256": g.digest.hex() if g.digest else None,
                    "empty_files_zone": g.zone,
                    "recoverable": g.recoverable,
                    "keep_reasons": list(g.keep_reasons),
                    "copies": [
                        {
                            "path": c.rel_path_display,
                            "keep": c in g.keep,
                            "protection": c.protection,
                        }
                        for c in g.copies
                    ],
                }
                for g in report.groups
            ],
        },
    )


@router.get("/scans/{scan_id}/search.csv")
def search_csv(scan_id: int, conn: Db, form: Annotated[SearchForm, Query()]) -> Response:
    _scan(conn, scan_id)
    try:
        spec = spec_from_form(form)
        compiled = compile_filter(spec)
    except FilterError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = search(
        conn, scan_id, compiled, limit=MAX_SEARCH_EXPORT, sort=Sort(form.sort), time_budget=60.0
    )
    rows: list[Sequence[Any]] = [
        (m.rel_path_display, m.kind, m.size, m.mtime_ns) for m in result.examples
    ]
    notes = [("# filtre", describe(spec))]
    if result.partial_reason:
        notes.append(("# résultat partiel", result.partial_reason))
    if result.total > len(result.examples):
        notes.append(("# tronqué", f"{len(result.examples)} sur {result.total}"))
    return _csv(
        f"treeaudit-scan{scan_id}-search.csv",
        ["path", "kind", "size", "mtime_ns"],
        [*rows, *[(a, b, "", "") for a, b in notes]],
    )
