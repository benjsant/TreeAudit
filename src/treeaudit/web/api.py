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
from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig, RootConfig
from treeaudit.core.datadir import own_dirs
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.db.connection import connect, from_sqlite_int
from treeaudit.duplicates.groups import load_groups
from treeaudit.duplicates.report import SelectionError, override_keep
from treeaudit.organize.plan import CATEGORIES, AdjustmentError
from treeaudit.parasites.cached import parasite_findings
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.scanner.browse import Listing, list_folders
from treeaudit.scanner.walker import SubpathError
from treeaudit.shellcmd import Action, cleanup_command, is_displayable
from treeaudit.web.explorer import Basket, resolve_basket
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


def browse_root(config: AppConfig, root: RootConfig, path: str) -> Listing:
    """Sub-folders offered in the picker: never TreeAudit's own, nor private ones."""
    return list_folders(
        root.path,
        path,
        own_dirs=own_dirs(config.data_dir, config.archive_dir),
        private_paths=root.private_paths,
    )


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
        listing = browse_root(get_config(request), status.root, path)
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
        "SELECT rel_path, kind, dev, ino FROM entry WHERE scan_id = ? AND entry_id = ?",
        (scan_id, entry_id),
    ).fetchone()
    if scan is None or row is None:
        raise HTTPException(404, "dossier inconnu")
    if row["kind"] != "dir":
        raise HTTPException(422, "seuls les dossiers peuvent être archivés")
    status = accepted_root(request, scan["root_id"])
    if scan["root_path"] != status.root.path:
        raise HTTPException(409, "cette analyse a été faite sur un autre dossier : relance-la")
    subpath = os.fsdecode(bytes(row["rel_path"]))
    expected = (from_sqlite_int(row["dev"]), from_sqlite_int(row["ino"]))
    return {"job": _start_archive(jobs, scan["root_id"], subpath, expected).snapshot()}


def _start_archive(
    jobs: JobManager, root_id: str, subpath: str, expected: tuple[int, int] | None = None
) -> Job:
    try:
        return jobs.start_archive(root_id, subpath, expected_folder=expected)
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


class BasketBody(BaseModel):
    entries: list[int] = Field(min_length=1, max_length=MAX_SELECTION)


class BasketCommand(BasketBody):
    action: Literal["trash", "delete", "archive"] = "trash"


def _basket(
    conn: sqlite3.Connection, scan_id: int, entries: list[int]
) -> tuple[sqlite3.Row, Basket]:
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "scan inconnu")
    return scan, resolve_basket(conn, scan_id, entries)


@router.post("/scans/{scan_id}/basket")
def basket_content(scan_id: int, body: BasketBody, conn: Db) -> dict[str, Any]:
    """What the basket holds, read back from the index (D-036)."""
    _, basket = _basket(conn, scan_id, body.entries)
    return {
        "items": [
            {
                "entry": item.entry_id,
                "path": item.rel_path_display,
                "kind": item.kind,
                "size": item.allocated,
                "protection": item.protection,
                "readable": is_displayable(item.rel_path),
            }
            for item in basket.items
        ],
        "total": basket.total,
        "unknown_size": basket.unknown_size,
        "unknown": list(basket.unknown),
        "covered": list(basket.covered),
        "root_refused": basket.root_refused,
    }


@router.post("/scans/{scan_id}/basket/command")
def basket_command(scan_id: int, body: BasketCommand, conn: Db, config: Config) -> dict[str, Any]:
    """Command for the basket. Paths come from the index, never from the page."""
    scan, basket = _basket(conn, scan_id, body.entries)
    warnings: list[str] = []
    if basket.root_refused:
        warnings.append("Le dossier analysé entier n'est jamais proposé : il a été retiré.")
    if basket.unknown:
        warnings.append(
            f"{len(basket.unknown)} élément(s) n'existent pas dans cette analyse : ignorés."
        )
    if not basket.items:
        raise HTTPException(422, "Le panier est vide.")
    for item in basket.items:
        if item.protection:
            warnings.append(f"Protégé ({item.protection}) : {item.rel_path_display}")
    if scan["status"] != "complete":
        warnings.append("L'analyse est incomplète : vérifie les éléments avant de lancer.")
    warnings.append(
        "Commande préparée d'après l'analyse : si le disque a changé depuis, relance une analyse."
    )
    result = cleanup_command(
        Action(body.action),
        basket.items,
        root_display(config, scan),
        archive_name=f"scan{scan_id}-panier.tar.gz",
    )
    return {
        "command": result.text,
        "count": len(basket.items),
        "freed": basket.total,
        "skipped": [item.rel_path_display for item in result.skipped],
        "warnings": warnings,
    }


@router.post("/quit")
def quit_server(request: Request, jobs: Jobs) -> dict[str, bool]:
    """Stop TreeAudit (native mode only). A running job is cancelled first:
    an archive in progress is then removed, never left half written."""
    stop = request.app.state.quit
    if stop is None:
        raise HTTPException(404, "TreeAudit s'arrête depuis le terminal qui l'a lancé")
    jobs.cancel()
    stop()
    return {"stopping": True}


class SuggestionsStart(BaseModel):
    dir: int | None = Field(default=None, ge=1)
    category: str = Field(max_length=40)
    query: str = Field(default="", max_length=200_000)
    """The tidying page's adjustments, so that the suggestions fit its plan."""


@router.post("/scans/{scan_id}/organize/suggestions", status_code=202)
def start_suggestions(
    scan_id: int, body: SuggestionsStart, request: Request, jobs: Jobs, conn: Db
) -> dict[str, Any]:
    """Expert mode (D-040, D-041): the local AI proposes sub-folders for one
    category of a tidying plan. A job: the result is shown, never applied."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(409, "mode expert inactif (TREEAUDIT_EXPERT=1, version Docker)")
    if scans_db.get_scan(conn, scan_id) is None:
        raise HTTPException(404, "scan inconnu")
    if body.category not in CATEGORIES:
        raise HTTPException(422, "catégorie inconnue")
    if body.dir is None:
        row = conn.execute(
            "SELECT entry_id FROM entry WHERE scan_id = ? AND depth = 0", (scan_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT entry_id FROM entry WHERE scan_id = ? AND entry_id = ? AND kind = 'dir'",
            (scan_id, body.dir),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "dossier inconnu")
    try:
        job = jobs.start_suggestions(llm, scan_id, row["entry_id"], body.category, body.query)
    except AdjustmentError as exc:
        raise HTTPException(422, f"ajustement refusé : {exc}") from exc
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job": job.snapshot()}


class OpinionsStart(BaseModel):
    min_confidence: int = Field(default=30, ge=0, le=100)


@router.post("/scans/{scan_id}/parasites/opinions", status_code=202)
def start_opinions(
    scan_id: int, body: OpinionsStart, request: Request, jobs: Jobs, conn: Db
) -> dict[str, Any]:
    """Expert mode (D-040): the local AI's opinion on the folders to check. A job:
    the opinions are shown, they never change the selection nor the classes."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(409, "mode expert inactif (TREEAUDIT_EXPERT=1, version Docker)")
    if scans_db.get_scan(conn, scan_id) is None:
        raise HTTPException(404, "scan inconnu")
    try:
        job = jobs.start_opinions(llm, scan_id, body.min_confidence)
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job": job.snapshot()}
