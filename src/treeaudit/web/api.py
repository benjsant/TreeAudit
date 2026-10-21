"""JSON API used by the pages' JavaScript.

Roots are designated by their id, scans and runs by their number, folders by a
path relative to a root: no route accepts an absolute path. State-changing
routes are POST and go through the CSRF guard (web/security.py).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from treeaudit.archive.destination import ArchiveDirStatus
from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.db.connection import connect
from treeaudit.duplicates.groups import load_groups
from treeaudit.duplicates.report import SelectionError, override_keep
from treeaudit.parasites.cached import parasite_findings
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.scanner.browse import list_folders
from treeaudit.scanner.walker import SubpathError
from treeaudit.shellcmd import Action, cleanup_command
from treeaudit.web.jobs import Job, JobError, JobManager

RootId = Annotated[str, Field(pattern=f"^{ROOT_ID_PATTERN.pattern}$")]
MAX_SELECTION = 100_000

router = APIRouter(prefix="/api")


def get_config(request: Request) -> AppConfig:
    config: AppConfig = request.app.state.config
    return config


def get_jobs(request: Request) -> JobManager:
    jobs: JobManager = request.app.state.jobs
    return jobs


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(get_config(request).data_dir)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]
Config = Annotated[AppConfig, Depends(get_config)]
Jobs = Annotated[JobManager, Depends(get_jobs)]


def accepted_root(request: Request, root_id: str) -> RootStatus:
    statuses: dict[str, RootStatus] = request.app.state.statuses
    status = statuses.get(root_id)
    if status is None:
        raise HTTPException(404, "racine inconnue")
    if not status.accepted:
        raise HTTPException(409, f"racine refusée : {REASON_MESSAGES[status.reason]}")  # type: ignore[index]
    return status


def root_display(config: AppConfig, scan: sqlite3.Row) -> str:
    root = config.get_root(scan["root_id"])
    return root.shown_path if root is not None else str(scan["root_path"])


@router.get("/roots/{root_id}/folders")
def folders(
    request: Request,
    root_id: Annotated[str, PathParam(pattern=f"^{ROOT_ID_PATTERN.pattern}$")],
    path: Annotated[str, Query(max_length=4096)] = "",
) -> dict[str, Any]:
    status = accepted_root(request, root_id)
    try:
        listing = list_folders(status.root.path, path)
    except SubpathError as exc:
        raise HTTPException(422, "chemin invalide") from exc
    except OSError as exc:
        raise HTTPException(404, f"dossier inaccessible : {exc.strerror or 'erreur'}") from exc
    return {
        "path": listing.path,
        "folders": list(listing.folders),
        "truncated": listing.truncated,
        "hidden": listing.hidden,
    }


class ScanStart(BaseModel):
    root_id: RootId
    subpath: str = Field(default="", max_length=4096)


class DupesStart(BaseModel):
    micro: bool = False


@router.post("/scans", status_code=202)
def start_scan(body: ScanStart, request: Request, jobs: Jobs) -> dict[str, Any]:
    accepted_root(request, body.root_id)
    try:
        job = jobs.start_scan(body.root_id, body.subpath)
    except SubpathError as exc:
        raise HTTPException(422, "sous-dossier invalide") from exc
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job": job.snapshot()}


@router.post("/scans/{scan_id}/dupes", status_code=202)
def start_dupes(scan_id: int, body: DupesStart, jobs: Jobs, conn: Db) -> dict[str, Any]:
    if scans_db.get_scan(conn, scan_id) is None:
        raise HTTPException(404, "scan inconnu")
    try:
        job = jobs.start_dupes(scan_id, micro=body.micro)
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job": job.snapshot()}


def archive_enabled(request: Request) -> None:
    status: ArchiveDirStatus = request.app.state.archive_status
    if not status.enabled:
        raise HTTPException(409, f"archivage indisponible : {status.message}")


class ArchiveStart(BaseModel):
    root_id: RootId
    subpath: str = Field(default="", max_length=4096)


@router.post("/archives", status_code=202)
def start_archive(body: ArchiveStart, request: Request, jobs: Jobs) -> dict[str, Any]:
    """Archive a folder chosen in the folder picker (names shown there are valid UTF-8)."""
    archive_enabled(request)
    accepted_root(request, body.root_id)
    return {"job": _start_archive(jobs, body.root_id, body.subpath).snapshot()}


@router.post("/scans/{scan_id}/entries/{entry_id}/archive", status_code=202)
def start_archive_of_entry(
    scan_id: int, entry_id: int, request: Request, jobs: Jobs, conn: Db
) -> dict[str, Any]:
    """Archive a folder seen in an analysis. Its raw name comes from the index, so
    folders whose name is not valid UTF-8 can be archived too (D-014)."""
    archive_enabled(request)
    scan = scans_db.get_scan(conn, scan_id)
    row = conn.execute(
        "SELECT rel_path, kind FROM entry WHERE scan_id = ? AND entry_id = ?",
        (scan_id, entry_id),
    ).fetchone()
    if scan is None or row is None:
        raise HTTPException(404, "dossier inconnu")
    if row["kind"] != "dir":
        raise HTTPException(422, "seuls les dossiers peuvent être archivés")
    accepted_root(request, scan["root_id"])
    subpath = os.fsdecode(bytes(row["rel_path"]))
    return {"job": _start_archive(jobs, scan["root_id"], subpath).snapshot()}


def _start_archive(jobs: JobManager, root_id: str, subpath: str) -> Job:
    try:
        return jobs.start_archive(root_id, subpath)
    except SubpathError as exc:
        raise HTTPException(422, "sous-dossier invalide") from exc
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs/current")
def current_job(jobs: Jobs) -> dict[str, Any]:
    job = jobs.current
    return {"job": None if job is None else job.snapshot()}


@router.post("/jobs/current/cancel")
def cancel_job(jobs: Jobs) -> dict[str, bool]:
    return {"cancelled": jobs.cancel()}


class ParasiteCommand(BaseModel):
    numbers: list[int] = Field(min_length=1, max_length=MAX_SELECTION)
    action: Literal["trash", "delete", "archive"] = "trash"
    min_confidence: int = Field(default=30, ge=0, le=100)


@router.post("/scans/{scan_id}/parasites/command")
def parasite_command(
    scan_id: int, body: ParasiteCommand, conn: Db, config: Config
) -> dict[str, Any]:
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "scan inconnu")
    findings = parasite_findings(conn, scan_id, min_confidence=body.min_confidence)
    by_number = {f.number: f for f in findings}
    unknown = sorted(set(body.numbers) - set(by_number))
    if unknown:
        raise HTTPException(422, f"détections inconnues : {unknown[:10]}")
    selected = [by_number[n] for n in sorted(set(body.numbers))]
    result = cleanup_command(
        Action(body.action),
        selected,
        root_display(config, scan),
        archive_name=f"scan{scan_id}-parasites.tar.gz",
    )
    return {
        "command": result.text,
        "count": len(selected),
        "skipped": [f.rel_path_display for f in result.skipped],
        "outside_green": [f.number for f in selected if f.band.value != "green"],
    }


class DupesCommand(BaseModel):
    groups: list[int] = Field(min_length=1, max_length=MAX_SELECTION)
    keep: dict[int, int] = Field(default_factory=dict)
    """Group number -> 1-based index of the copy to keep instead of the suggestion."""
    action: Literal["trash", "delete"] = "trash"


@router.post("/dupes/{run_id}/command")
def dupes_command(run_id: int, body: DupesCommand, conn: Db, config: Config) -> dict[str, Any]:
    run = dupes_db.get_run(conn, run_id)
    scan = scans_db.get_scan(conn, run["scan_id"]) if run is not None else None
    if run is None or scan is None:
        raise HTTPException(404, "analyse inconnue")
    report = load_groups(conn, run_id)
    by_number = {g.number: g for g in report.groups}
    unknown = sorted(set(body.groups) - set(by_number))
    if unknown:
        raise HTTPException(422, f"groupes inconnus : {unknown[:10]}")
    selected = []
    try:
        for number in sorted(set(body.groups)):
            group = by_number[number]
            if number in body.keep:
                group = override_keep(group, body.keep[number])
            selected.append(group)
    except SelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    targets = [copy for group in selected for copy in group.removable]
    result = cleanup_command(Action(body.action), targets, root_display(config, scan))
    return {
        "command": result.text,
        "count": len(targets),
        "freed": sum(g.recoverable for g in selected),
        "skipped": [c.rel_path_display for c in result.skipped],
    }
