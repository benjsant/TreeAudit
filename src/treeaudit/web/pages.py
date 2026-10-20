"""HTML pages (server-rendered, French UI).

File names come from audited disks and are untrusted: templates are rendered
with autoescaping on and StrictUndefined, never with ``|safe``; the JavaScript
only uses ``textContent`` (see static/app.js) and the CSP forbids inline code.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import jinja2
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from treeaudit import __version__
from treeaudit.core.config import ROOT_ID_PATTERN
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.duplicates.groups import load_groups
from treeaudit.filters.search import Sort, search
from treeaudit.filters.spec import FilterError, compile_filter, describe
from treeaudit.parasites.commands import recreate_command
from treeaudit.parasites.detect import Band, find_parasites
from treeaudit.parasites.report import BAND_LABELS, BAND_MEANING, human_size
from treeaudit.parasites.rules import load_default_rules
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.scanner.browse import list_folders
from treeaudit.scanner.walker import SubpathError, parse_subpath
from treeaudit.web import queries
from treeaudit.web.api import Config, Db, Jobs, root_display
from treeaudit.web.search_form import SearchForm, spec_from_form

TEMPLATES_DIR = Path(__file__).parent / "templates"

STATUS_LABELS = {
    "complete": "complet",
    "incomplete": "incomplet",
    "cancelled": "annulé",
    "failed": "échec",
    "running": "en cours",
    "pending": "en attente",
}


def _date(ns_or_iso: int | str | None) -> str:
    if ns_or_iso is None:
        return "-"
    if isinstance(ns_or_iso, str):
        return ns_or_iso.replace("T", " ").replace("+00:00", " UTC")
    return datetime.fromtimestamp(ns_or_iso / 1e9, UTC).strftime("%Y-%m-%d")


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
    env.filters["status"] = lambda value: STATUS_LABELS.get(value, value)
    env.globals["version"] = __version__
    return env


templates = Jinja2Templates(env=_environment())
router = APIRouter(default_response_class=HTMLResponse)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    jobs = request.app.state.jobs
    job = jobs.current
    context.setdefault("running_job", job.snapshot() if job is not None and job.running else None)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def error_page(request: Request, status_code: int, message: str) -> Response:
    return render(request, "error.html", status_code, code=status_code, message=message)


def _scan_or_404(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row:
    scan = scans_db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "Scan inconnu.")
    return scan


@router.get("/")
def index(request: Request, conn: Db) -> Response:
    statuses: dict[str, RootStatus] = request.app.state.statuses
    roots = [
        {
            "id": s.root.id,
            "label": s.root.label,
            "accepted": s.accepted,
            "message": None if s.reason is None else REASON_MESSAGES[s.reason],
        }
        for s in statuses.values()
    ]
    runs = []
    for run in dupes_db.list_runs(conn, limit=20):
        options = json.loads(run["options"])
        runs.append({**dict(run), "micro": bool(options.get("micro"))})
    return render(
        request,
        "index.html",
        roots=roots,
        scans=scans_db.list_scans(conn, limit=30),
        runs=runs,
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
        raise HTTPException(404, "Racine inconnue ou refusée.")
    try:
        parts = parse_subpath(path)
        listing = list_folders(status.root.path, path)
    except SubpathError as exc:
        raise HTTPException(422, "Chemin invalide.") from exc
    except OSError as exc:
        raise HTTPException(404, f"Dossier inaccessible : {exc.strerror or 'erreur'}.") from exc
    crumbs = [("/".join(parts[: i + 1]), part) for i, part in enumerate(parts)]
    return render(
        request,
        "scan_new.html",
        roots=accepted,
        root=status.root,
        listing=listing,
        crumbs=crumbs,
        parent="/".join(parts[:-1]) if parts else None,
    )


@router.get("/jobs/current")
def job_page(request: Request, jobs: Jobs) -> Response:
    job = jobs.current
    return render(request, "job.html", job=None if job is None else job.snapshot())


@router.get("/scans/{scan_id}")
def scan_page(
    request: Request,
    scan_id: int,
    conn: Db,
    dir: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    scan = _scan_or_404(conn, scan_id)
    node = (
        queries.get_node(conn, scan_id, dir)
        if dir is not None
        else queries.start_node(conn, scan_id)
    )
    if node is None or node.kind != "dir":
        raise HTTPException(404, "Dossier inconnu dans ce scan.")
    kids, total_kids = queries.children(conn, scan_id, node.entry_id)
    return render(
        request,
        "scan.html",
        scan=scan,
        node=node,
        crumbs=queries.breadcrumb(conn, scan_id, node),
        children=kids,
        total_children=total_kids,
        extensions=scans_db.top_extensions(conn, scan_id, limit=15),
        errors=scans_db.list_errors(conn, scan_id, limit=50),
        exclusions=scans_db.list_exclusions(conn, scan_id, limit=50),
        runs=[r for r in dupes_db.list_runs(conn, limit=200) if r["scan_id"] == scan_id],
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
    findings = find_parasites(conn, scan_id, load_default_rules(), min_confidence=min_confidence)
    rows = [{"finding": f, "recreate": recreate_command(f, display)} for f in findings]
    totals = {
        band: (
            sum(1 for f in findings if f.band is band),
            sum(f.allocated_size or 0 for f in findings if f.band is band),
        )
        for band in Band
    }
    return render(
        request,
        "parasites.html",
        scan=scan,
        rows=rows,
        totals=totals,
        bands=list(Band),
        band_labels=BAND_LABELS,
        band_meaning=BAND_MEANING,
        min_confidence=min_confidence,
    )


@router.get("/dupes/{run_id}")
def dupes_page(
    request: Request,
    run_id: int,
    conn: Db,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 50,
    copies: Annotated[int, Query(ge=1, le=10_000)] = 10,
) -> Response:
    run = dupes_db.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, "Analyse inconnue.")
    report = load_groups(conn, run_id)
    return render(
        request,
        "dupes.html",
        run=run,
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
    request: Request, scan_id: int, conn: Db, form: Annotated[SearchForm, Query()]
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
        form=form,
        result=result,
        error=error,
        description=description,
        query=request.url.query,
    )
