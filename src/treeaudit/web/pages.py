"""HTML pages (server-rendered, French UI, plain words).

The interface is a guided path: choose a folder, analyse it, read a short
summary, then clean up. Technical vocabulary (rules, digests, exclusions,
errors) stays in folded "details" sections.

File names come from audited disks and are untrusted: templates are rendered
with autoescaping on and StrictUndefined, never with ``|safe``; the JavaScript
only uses ``textContent`` (see static/app.js) and the CSP forbids inline code.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import jinja2
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from treeaudit import __version__
from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.duplicates.groups import load_groups
from treeaudit.filters.search import Sort, search
from treeaudit.filters.spec import FilterError, compile_filter, describe
from treeaudit.parasites.cached import parasite_findings
from treeaudit.parasites.commands import recreate_command
from treeaudit.parasites.detect import Band
from treeaudit.parasites.report import human_size
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.scanner.browse import list_folders
from treeaudit.scanner.walker import SubpathError, parse_subpath
from treeaudit.web import queries, summaries
from treeaudit.web.api import Config, Db, Jobs, root_display
from treeaudit.web.search_form import SearchForm, spec_from_form

TEMPLATES_DIR = Path(__file__).parent / "templates"

STATUS_LABELS = {
    "complete": "terminée",
    "incomplete": "incomplète",
    "cancelled": "annulée",
    "failed": "échouée",
    "running": "en cours",
    "pending": "en attente",
}
_MONTHS = (
    "janv.",
    "févr.",
    "mars",
    "avr.",
    "mai",
    "juin",
    "juil.",
    "août",
    "sept.",
    "oct.",
    "nov.",
    "déc.",
)


def _date(value: int | str | None) -> str:
    """'20 oct. 2026' from a scan timestamp (ISO text) or an mtime in nanoseconds."""
    if value is None:
        return "-"
    if isinstance(value, str):
        moment = datetime.fromisoformat(value)
    else:
        moment = datetime.fromtimestamp(value / 1e9, UTC)
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


_THIN_SPACE = chr(0x202F)  # French thousands separator


def _number(value: int) -> str:
    """150355 -> '150 355' (French thousands separator)."""
    return f"{value:,}".replace(",", _THIN_SPACE)


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,  # every template, whatever its extension
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["size"] = human_size
    env.filters["date"] = _date
    env.filters["number"] = _number
    env.filters["status"] = lambda value: STATUS_LABELS.get(value, value)
    env.globals["version"] = __version__
    return env


templates = Jinja2Templates(env=_environment())
router = APIRouter(default_response_class=HTMLResponse)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    jobs = request.app.state.jobs
    job = jobs.current
    context.setdefault("running_job", job.snapshot() if job is not None and job.running else None)
    archive = request.app.state.archive_status
    context.setdefault(
        "archive",
        {"enabled": archive.enabled, "where": archive.shown_path, "message": archive.message},
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def error_page(request: Request, status_code: int, message: str) -> Response:
    return render(request, "error.html", status_code, code=status_code, message=message)


def _scan_or_404(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row:
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "Cette analyse n'existe pas.")
    return scan


def analysis_title(config: AppConfig, scan: sqlite3.Row) -> str:
    """Short name of what was analysed: the folder name, as the user knows it."""
    shown = root_display(config, scan).rstrip("/")
    if scan["subpath"]:
        shown = f"{shown}/{scan['subpath']}"
    return os.path.basename(shown) or shown or str(scan["root_id"])


def _latest_run(conn: sqlite3.Connection, scan_id: int, *, micro: bool) -> sqlite3.Row | None:
    for run in dupes_db.list_runs(conn, limit=500):
        if run["scan_id"] == scan_id and bool(json.loads(run["options"]).get("micro")) == micro:
            return run
    return None


@router.get("/")
def index(request: Request, conn: Db, config: Config) -> Response:
    statuses: dict[str, RootStatus] = request.app.state.statuses
    refused = [
        {"label": s.root.label, "message": REASON_MESSAGES[s.reason]}
        for s in statuses.values()
        if s.reason is not None
    ]
    analyses = [
        {"scan": scan, "title": analysis_title(config, scan)}
        for scan in scans_db.list_scans(conn, limit=30)
    ]
    return render(
        request,
        "index.html",
        analyses=analyses,
        refused=refused,
        can_analyse=any(s.accepted for s in statuses.values()),
    )


@router.get("/scan/new")
def new_scan(
    request: Request,
    root: Annotated[str | None, Query(pattern=f"^{ROOT_ID_PATTERN.pattern}$")] = None,
    path: Annotated[str, Query(max_length=4096)] = "",
) -> Response:
    statuses: dict[str, RootStatus] = request.app.state.statuses
    accepted = [s for s in statuses.values() if s.accepted]
    if root is None:
        if len(accepted) == 1:
            root = accepted[0].root.id
        else:
            return render(request, "scan_new.html", roots=accepted, root=None)
    status = statuses.get(root)
    if status is None or not status.accepted:
        raise HTTPException(404, "Ce dossier n'est pas autorisé.")
    try:
        parts = parse_subpath(path)
        listing = list_folders(status.root.path, path)
    except SubpathError as exc:
        raise HTTPException(422, "Chemin invalide.") from exc
    except OSError as exc:
        raise HTTPException(404, f"Dossier inaccessible : {exc.strerror or 'erreur'}.") from exc
    crumbs = [("/".join(parts[: i + 1]), part) for i, part in enumerate(parts)]
    root_name = os.path.basename(status.root.shown_path.rstrip("/")) or status.root.label
    return render(
        request,
        "scan_new.html",
        roots=accepted,
        root=status.root,
        root_name=root_name,
        listing=listing,
        crumbs=crumbs,
        current_name=parts[-1] if parts else root_name,
        parent="/".join(parts[:-1]) if parts else None,
    )


@router.get("/jobs/current")
def job_page(request: Request, jobs: Jobs) -> Response:
    job = jobs.current
    return render(request, "job.html", job=None if job is None else job.snapshot())


@router.get("/scans/{scan_id}")
def summary_page(request: Request, scan_id: int, conn: Db, config: Config) -> Response:
    """The summary: what to clean, duplicates, where the space goes."""
    scan = _scan_or_404(conn, scan_id)
    start = queries.start_node(conn, scan_id)
    biggest = queries.children(conn, scan_id, start.entry_id, limit=5)[0] if start else []
    parasites = summaries.parasites_summary(conn, scan_id)

    def dupes_summary(micro: bool) -> dict[str, Any] | None:
        run = _latest_run(conn, scan_id, micro=micro)
        if run is None:
            return None
        return {"run": run, **summaries.dupes_summary(conn, run["id"])}

    return render(
        request,
        "scan.html",
        scan=scan,
        title=analysis_title(config, scan),
        start=start,
        biggest=biggest,
        safe_count=parasites["safe_count"],
        safe_size=parasites["safe_size"],
        check_count=parasites["check_count"],
        check_size=parasites["check_size"],
        dupes=dupes_summary(micro=False),
        micro=dupes_summary(micro=True),
        extensions=scans_db.top_extensions(conn, scan_id, limit=15),
        errors=scans_db.list_errors(conn, scan_id, limit=50),
        exclusions=scans_db.list_exclusions(conn, scan_id, limit=50),
    )


@router.get("/scans/{scan_id}/space")
def space_page(
    request: Request,
    scan_id: int,
    conn: Db,
    config: Config,
    dir: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    scan = _scan_or_404(conn, scan_id)
    if dir is None:
        node = queries.start_node(conn, scan_id)
    else:
        node = queries.get_node(conn, scan_id, dir)
    if node is None or node.kind != "dir":
        raise HTTPException(404, "Ce dossier n'existe pas dans cette analyse.")
    kids, total_kids = queries.children(conn, scan_id, node.entry_id)
    return render(
        request,
        "space.html",
        scan=scan,
        title=analysis_title(config, scan),
        node=node,
        crumbs=queries.breadcrumb(conn, scan_id, node),
        children=kids,
        total_children=total_kids,
    )


@router.get("/scans/{scan_id}/parasites")
def parasites_page(
    request: Request,
    scan_id: int,
    conn: Db,
    config: Config,
    min_confidence: Annotated[int, Query(ge=0, le=100)] = 30,
) -> Response:
    scan = _scan_or_404(conn, scan_id)
    display = root_display(config, scan)
    findings = parasite_findings(conn, scan_id, min_confidence=min_confidence)
    rows: list[dict[str, Any]] = [
        {"finding": f, "recreate": recreate_command(f, display)} for f in findings
    ]
    return render(
        request,
        "parasites.html",
        scan=scan,
        title=analysis_title(config, scan),
        safe=[r for r in rows if r["finding"].band is Band.GREEN],
        check=[r for r in rows if r["finding"].band is Band.ORANGE],
        doubt=[r for r in rows if r["finding"].band is Band.RED],
        min_confidence=min_confidence,
    )


@router.get("/dupes/{run_id}")
def dupes_page(
    request: Request,
    run_id: int,
    conn: Db,
    config: Config,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 50,
    copies: Annotated[int, Query(ge=1, le=10_000)] = 10,
) -> Response:
    run = dupes_db.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, "Cette recherche de doublons n'existe pas.")
    scan = _scan_or_404(conn, run["scan_id"])
    report = load_groups(conn, run_id)
    return render(
        request,
        "dupes.html",
        run=run,
        scan=scan,
        title=analysis_title(config, scan),
        report=report,
        groups=report.groups[:limit],
        limit=limit,
        copies=copies,
        removable=sum(len(g.removable) for g in report.groups),
        recoverable=sum(g.recoverable for g in report.groups),
        errors=dupes_db.list_errors(conn, run_id, limit=50),
    )


@router.get("/scans/{scan_id}/search")
def search_page(
    request: Request,
    scan_id: int,
    conn: Db,
    config: Config,
    form: Annotated[SearchForm, Query()],
) -> Response:
    scan = _scan_or_404(conn, scan_id)
    result = None
    error = None
    description = None
    if form.submitted:
        try:
            spec = spec_from_form(form)
            compiled = compile_filter(spec)
            description = describe(spec)
            result = search(
                conn, scan_id, compiled, limit=form.limit, sort=Sort(form.sort), time_budget=30.0
            )
        except FilterError as exc:
            error = str(exc)
    return render(
        request,
        "search.html",
        scan=scan,
        title=analysis_title(config, scan),
        form=form,
        advanced_open=form.has_advanced,
        result=result,
        error=error,
        description=description,
        query=request.url.query,
    )
