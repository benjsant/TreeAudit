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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, model_validator

from treeaudit.archive.destination import ArchiveDirStatus
from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig, RootConfig
from treeaudit.core.datadir import own_dirs
from treeaudit.db import dupes as dupes_db
from treeaudit.db import images as images_db
from treeaudit.db import scans as scans_db
from treeaudit.db.connection import connect, from_sqlite_int
from treeaudit.duplicates.groups import guarded_places, load_groups
from treeaudit.duplicates.reader import Expected
from treeaudit.duplicates.report import SelectionError, override_keep
from treeaudit.duplicates.runner import expected_root_of
from treeaudit.parasites.cached import parasite_findings
from treeaudit.preview.images import (
    IMAGE_EXTENSIONS,
    MAX_PREVIEW_SIZE,
    PreviewError,
    previewable,
    read_image,
)
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.scanner.browse import Listing, list_folders
from treeaudit.scanner.walker import SubpathError
from treeaudit.shellcmd import Action, CleanupCommand, cleanup_command, is_displayable
from treeaudit.similar import images as similar_images
from treeaudit.web.explorer import Basket, resolve_basket
from treeaudit.web.jobs import Job, JobError, JobManager
from treeaudit.web.live import refresh_roots

RootId = Annotated[str, Field(pattern=f"^{ROOT_ID_PATTERN.pattern}$")]
MAX_SELECTION = 100_000

router = APIRouter(prefix="/api")


def get_config(request: Request) -> AppConfig:
    """The configuration the server started with (dependency)."""
    config: AppConfig = request.app.state.config
    return config


def get_jobs(request: Request) -> JobManager:
    """The one manager of background jobs (dependency)."""
    jobs: JobManager = request.app.state.jobs
    return jobs


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """A connection to the index for this request only, closed after it (dependency)."""
    # FastAPI may open this in one worker thread and run the route in another:
    # found when a gallery asked for many images at once (D-046). The
    # connection belongs to this request alone, used by one thread at a time.
    conn = connect(get_config(request).data_dir, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]
Config = Annotated[AppConfig, Depends(get_config)]
Jobs = Annotated[JobManager, Depends(get_jobs)]


def accepted_root(request: Request, root_id: str) -> RootStatus:
    """The root, if it is known and accepted right now (disks come and go:
    the live statuses are read); 404 or 409 otherwise. A root not accepted at
    the last look is looked for once more: its disk may just have been
    plugged in again, with a link to it still open (native mode)."""
    statuses: dict[str, RootStatus] = request.app.state.statuses
    status = statuses.get(root_id)
    if status is None or not status.accepted:
        refresh_roots(request.app)
        statuses = request.app.state.statuses
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
    """The root of a scan as the user knows it, to write in commands; the
    recorded path if the root is no longer configured."""
    root = config.get_root(scan["root_id"])
    return root.shown_path if root is not None else str(scan["root_path"])


@router.get("/roots/{root_id}/folders")
def folders(
    request: Request,
    root_id: Annotated[str, PathParam(pattern=f"^{ROOT_ID_PATTERN.pattern}$")],
    path: Annotated[str, Query(max_length=4096)] = "",
) -> dict[str, Any]:
    """The sub-folders of a folder of an accepted root, for the folder picker.
    ``path`` is relative to the root and checked like a scan's sub-folder."""
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
    """Body of a new scan: an accepted root and, optionally, one of its sub-folders."""

    root_id: RootId
    subpath: str = Field(default="", max_length=4096)


class DupesStart(BaseModel):
    """Body of a new search for duplicates: ``micro`` for small files (D-026)."""

    micro: bool = False


@router.post("/scans", status_code=202)
def start_scan(body: ScanStart, request: Request, jobs: Jobs) -> dict[str, Any]:
    """Start a scan in the background; 409 if another job runs."""
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
    """Start a search for duplicates on a recorded scan; 409 if another job runs."""
    if scans_db.get_scan(conn, scan_id) is None:
        raise HTTPException(404, "scan inconnu")
    try:
        job = jobs.start_dupes(scan_id, micro=body.micro)
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job": job.snapshot()}


def archive_enabled(request: Request) -> None:
    """409 unless the archive folder was accepted at startup (D-032)."""
    status: ArchiveDirStatus = request.app.state.archive_status
    if not status.enabled:
        raise HTTPException(409, f"archivage indisponible : {status.message}")


class ArchiveStart(BaseModel):
    """Body of a new archive: a folder of an accepted root."""

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
    """The running or last job and its progress; None if there was none."""
    job = jobs.current
    return {"job": None if job is None else job.snapshot()}


@router.post("/jobs/current/cancel")
def cancel_job(jobs: Jobs) -> dict[str, bool]:
    """Ask the running job to stop; False when nothing runs."""
    return {"cancelled": jobs.cancel()}


def refused_warnings(result: CleanupCommand[Any]) -> list[str]:
    """One line per target the double check refused, with why (shellcmd)."""
    return [
        f"Refusé par la double vérification : {target.rel_path_display or '(dossier analysé)'}"
        f" — {reason}"
        for target, reason in result.refused
    ]


# An image is shown, never run: no script, no plugin, no frame, even if opened alone.
IMAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Content-Disposition": "inline",
}


@router.get("/scans/{scan_id}/entries/{entry_id}/image")
def entry_image(
    request: Request, scan_id: int, entry_id: int, conn: Db, config: Config
) -> Response:
    """The original bytes of an image of the analysis (D-046), read safely."""
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "scan inconnu")
    row = conn.execute(
        "SELECT rel_path, kind, extension, size, mtime_ns, dev, ino FROM entry "
        "WHERE scan_id = ? AND entry_id = ?",
        (scan_id, entry_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "élément inconnu")
    if not previewable(row["kind"], row["extension"], row["size"]):
        raise HTTPException(422, "pas d'aperçu pour cet élément")
    root = accepted_root(request, scan["root_id"]).root
    if root.path != scan["root_path"]:
        raise HTTPException(409, "la racine a changé depuis l'analyse")
    expected = Expected(
        dev=from_sqlite_int(row["dev"]),
        ino=from_sqlite_int(row["ino"]),
        size=row["size"],
        mtime_ns=row["mtime_ns"],
    )
    try:
        image = read_image(
            root.path,
            expected_root_of(conn, scan, root.path),
            bytes(row["rel_path"]),
            expected,
            private_paths=root.private_paths,
        )
    except (PreviewError, OSError) as exc:
        raise HTTPException(409, str(exc) or "aperçu impossible") from exc
    return Response(image.content, media_type=image.media_type, headers=IMAGE_HEADERS)


# --- similar images (D-069): signatures computed by the page --------------------------------

MAX_SIGNATURES = 500
"""Signatures per request: a page sends them as it goes, never all at once."""


def _final_scan(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row:
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "scan inconnu")
    if scan["status"] in ("pending", "running"):
        raise HTTPException(409, "l'analyse n'est pas terminée")
    return scan


@router.post("/scans/{scan_id}/images/todo")
def images_todo(scan_id: int, conn: Db) -> dict[str, Any]:
    """The images of the analysis still to sign, after reusing the signatures of
    unchanged files. POST: reusing them writes to the index."""
    _final_scan(conn, scan_id)
    state = images_db.pending(conn, scan_id, extensions=IMAGE_EXTENSIONS, max_size=MAX_PREVIEW_SIZE)
    return {
        "algo": images_db.ALGO,
        "total": state.total,
        "done": state.done,
        "failed": state.failed,
        "todo": list(state.todo),
    }


class SignatureItem(BaseModel):
    """One image: its signature, or why the page has none. Hex text only, of
    fixed length: the page is not trusted, the server checks everything."""

    entry: int = Field(ge=0)
    hash: str | None = Field(default=None, pattern=f"^[0-9a-f]{{{2 * images_db.HASH_BYTES}}}$")
    thumb: str | None = Field(default=None, pattern=f"^[0-9a-f]{{{2 * images_db.THUMB_BYTES}}}$")
    width: int | None = Field(default=None, ge=1, le=1_000_000)
    height: int | None = Field(default=None, ge=1, le=1_000_000)
    failure: Literal["undecodable", "unreadable"] | None = None

    @model_validator(mode="after")
    def _one_or_the_other(self) -> SignatureItem:
        signed = (self.hash, self.thumb, self.width, self.height)
        if self.failure is None and None in signed:
            raise ValueError("signature incomplète")
        if self.failure is not None and any(v is not None for v in signed):
            raise ValueError("une image est signée ou en échec, pas les deux")
        return self


class SignatureBody(BaseModel):
    """A batch of results, computed with one version of the signature."""

    algo: int
    items: list[SignatureItem] = Field(min_length=1, max_length=MAX_SIGNATURES)


@router.post("/scans/{scan_id}/images/signatures")
def save_signatures(scan_id: int, body: SignatureBody, conn: Db) -> dict[str, int]:
    """Store the signatures the page computed, all or nothing."""
    _final_scan(conn, scan_id)
    if body.algo != images_db.ALGO:
        raise HTTPException(409, "la page n'est pas à jour : recharge-la")
    signatures = [
        images_db.Signature(
            item.entry,
            bytes.fromhex(item.hash),
            bytes.fromhex(item.thumb),
            item.width,
            item.height,
        )
        for item in body.items
        if item.hash is not None
        and item.thumb is not None
        and item.width is not None
        and item.height is not None
    ]
    failures = [
        images_db.Failure(item.entry, item.failure)
        for item in body.items
        if item.failure is not None
    ]
    refused = images_db.save(
        conn,
        scan_id,
        signatures,
        failures,
        extensions=IMAGE_EXTENSIONS,
        max_size=MAX_PREVIEW_SIZE,
    )
    if refused:
        raise HTTPException(422, f"pas des images de cette analyse : {refused[:10]}")
    return {"saved": len(signatures), "failed": len(failures)}


class ParasiteCommand(BaseModel):
    """Findings chosen by their number, at the same confidence threshold as
    the page that showed them (the numbers depend on it)."""

    numbers: list[int] = Field(min_length=1, max_length=MAX_SELECTION)
    action: Literal["trash", "delete", "archive"] = "trash"
    min_confidence: int = Field(default=30, ge=0, le=100)


@router.post("/scans/{scan_id}/parasites/command")
def parasite_command(
    scan_id: int, body: ParasiteCommand, conn: Db, config: Config
) -> dict[str, Any]:
    """The command for the chosen findings, built from the index again: the
    page only sends numbers, never paths. TreeAudit never runs it (D-025)."""
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
        scanned=scan["subpath"],
        protected=config.protected_paths(),
        archive_name=f"scan{scan_id}-parasites.tar.gz",
    )
    return {
        "command": result.text,
        "count": len(selected),
        "skipped": [f.rel_path_display for f in result.skipped],
        "warnings": refused_warnings(result),
        "outside_green": [f.number for f in selected if f.band.value != "green"],
    }


class DupesCommand(BaseModel):
    """Groups chosen by their number, and the copy to keep where the user
    picked another one."""

    groups: list[int] = Field(min_length=1, max_length=MAX_SELECTION)
    keep: dict[int, int] = Field(default_factory=dict)
    """Group number -> 1-based index of the copy to keep instead of the suggestion."""
    action: Literal["trash", "delete"] = "trash"


@router.post("/dupes/{run_id}/command")
def dupes_command(run_id: int, body: DupesCommand, conn: Db, config: Config) -> dict[str, Any]:
    """The command removing every copy not kept in the chosen groups, built
    from the index again. Protected copies are always kept."""
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
    result = cleanup_command(
        Action(body.action),
        targets,
        root_display(config, scan),
        scanned=scan["subpath"],
        protected=config.protected_paths(),
    )
    return {
        "command": result.text,
        "count": len(targets),
        "freed": sum(g.recoverable for g in selected),
        "skipped": [c.rel_path_display for c in result.skipped],
        "warnings": refused_warnings(result),
    }


class ImagesCommand(BaseModel):
    """Groups of similar images chosen by their number, the image to keep where
    the user picked another one, and the version of the signatures the page saw."""

    groups: list[int] = Field(min_length=1, max_length=MAX_SELECTION)
    keep: dict[int, int] = Field(default_factory=dict)
    """Group number -> 1-based position of the image to keep instead of the suggestion."""
    action: Literal["trash", "delete"] = "trash"
    version: str = Field(max_length=64)


@router.post("/scans/{scan_id}/images/command")
def images_command(scan_id: int, body: ImagesCommand, conn: Db, config: Config) -> dict[str, Any]:
    """The command removing the images proposed in the chosen groups, built
    from the index again: only images directly like the kept one, never a
    protected one (D-069)."""
    scan = _final_scan(conn, scan_id)
    report = similar_images.similar_images(conn, scan_id)
    if body.version != report.version:
        raise HTTPException(409, "de nouvelles images ont été traitées depuis : recharge la page")
    unknown = sorted(n for n in set(body.groups) if not 1 <= n <= len(report.groups))
    if unknown:
        raise HTTPException(422, f"groupes inconnus : {unknown[:10]}")
    guarded = guarded_places(conn, scan_id)
    selected = []
    try:
        for number in sorted(set(body.groups)):
            group = report.groups[number - 1]
            if number in body.keep:
                group = similar_images.override_keep(group, body.keep[number], guarded)
            selected.append(group)
    except similar_images.SelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    targets = [member.image for group in selected for member in group.removable]
    if not targets:
        raise HTTPException(422, "Rien à proposer dans ces groupes.")
    result = cleanup_command(
        Action(body.action),
        targets,
        root_display(config, scan),
        scanned=scan["subpath"],
        protected=config.protected_paths(),
    )
    warnings = [
        "Ressemblantes ne veut pas dire identiques : les photos d'une rafale sont des prises "
        "différentes. Regarde les aperçus avant de lancer la commande."
    ]
    if scan["status"] != "complete":
        warnings.append("L'analyse est incomplète : vérifie les éléments avant de lancer.")
    return {
        "command": result.text,
        "count": len(targets),
        "freed": sum(image.size for image in targets),
        "skipped": [image.rel_path_display for image in result.skipped],
        "warnings": warnings + refused_warnings(result),
    }


class BasketBody(BaseModel):
    """Entries of the basket, by id in one analysis (D-036)."""

    entries: list[int] = Field(min_length=1, max_length=MAX_SELECTION)


class BasketCommand(BasketBody):
    """The basket and what to do with it."""

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
        scanned=scan["subpath"],
        protected=config.protected_paths(),
        archive_name=f"scan{scan_id}-panier.tar.gz",
    )
    warnings += refused_warnings(result)
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


@router.post("/scans/{scan_id}/forget")
def forget_scan(scan_id: int, jobs: Jobs, conn: Db) -> dict[str, bool]:
    """Erase what TreeAudit recorded for an analysis (D-043): its index, its
    duplicate analyses, its summaries. Never a file of the analysed folder."""
    job = jobs.current
    if job is not None and job.running:
        raise HTTPException(409, "une tâche est en cours : attends sa fin ou annule-la")
    if not scans_db.forget_scan(conn, scan_id):
        raise HTTPException(404, "analyse inconnue")
    return {"forgotten": True}


@router.post("/index/compact", status_code=202)
def compact_index(jobs: Jobs) -> dict[str, Any]:
    """Give the index's free space back to the disk, as a job."""
    try:
        job = jobs.start_compact()
    except JobError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job": job.snapshot()}
