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
from urllib.parse import quote

import jinja2
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from treeaudit import __version__
from treeaudit.archive.listing import list_archives
from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig, RootConfig
from treeaudit.db import dupes as dupes_db
from treeaudit.db import scans as scans_db
from treeaudit.duplicates.groups import load_groups
from treeaudit.filters.search import Sort, search
from treeaudit.filters.spec import FilterError, compile_filter, describe
from treeaudit.llm.client import LlmClient, LlmError
from treeaudit.llm.organize import suggest as suggest_subfolders
from treeaudit.organize import plan as organize_plan
from treeaudit.parasites.cached import parasite_findings
from treeaudit.parasites.commands import recreate_command
from treeaudit.parasites.detect import Band
from treeaudit.parasites.report import human_size
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES, RejectReason
from treeaudit.scanner.walker import SubpathError, parse_subpath
from treeaudit.shellcmd import host_path, safe_root
from treeaudit.web import explorer, queries, summaries
from treeaudit.web.api import Config, Db, Jobs, browse_root, root_display
from treeaudit.web.live import refresh_roots
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
        try:
            moment = datetime.fromtimestamp(value / 1e9, UTC)
        except (OverflowError, OSError, ValueError):  # a file dated year 300 000
            return "date invalide"
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


def _datetime(value: int) -> str:
    """'20 oct. 2026 à 14:05' (local time) from an mtime in nanoseconds."""
    try:
        moment = datetime.fromtimestamp(value / 1e9).astimezone()
    except (OverflowError, OSError, ValueError):
        return "date invalide"
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year} à {moment:%H:%M}"


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
    env.filters["datetime"] = _datetime
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
    context.setdefault("external_enabled", request.app.state.external_enabled)
    context.setdefault("can_quit", request.app.state.quit is not None)
    context.setdefault("live_disks", request.app.state.detect is not None)
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


def path_prefix(config: AppConfig, scan: sqlite3.Row) -> str:
    """Host path of the analysed root, ending with "/": shown before relative paths."""
    return root_display(config, scan).rstrip("/") + "/"


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
def index(request: Request, conn: Db) -> Response:
    refresh_roots(request.app)
    config: AppConfig = request.app.state.config
    statuses: dict[str, RootStatus] = request.app.state.statuses
    refused = [
        {"label": s.root.label, "message": REASON_MESSAGES[s.reason]}
        for s in statuses.values()
        # Not mounted in the container = not configured: nothing to report.
        if s.reason is not None and s.reason is not RejectReason.NOT_MOUNTED and not s.root.external
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
        can_analyse=any(s.accepted for s in statuses.values())
        or request.app.state.external_enabled,
    )


@router.get("/scan/new")
def new_scan(
    request: Request,
    root: Annotated[str | None, Query(pattern=f"^{ROOT_ID_PATTERN.pattern}$")] = None,
    path: Annotated[str, Query(max_length=4096)] = "",
) -> Response:
    if root is None:
        refresh_roots(request.app)  # the disks plugged in now (native mode)
    statuses: dict[str, RootStatus] = request.app.state.statuses
    accepted = [s for s in statuses.values() if s.accepted]
    external_enabled: bool = request.app.state.external_enabled
    if root is None:
        if len(accepted) == 1 and not external_enabled:
            root = accepted[0].root.id
        else:
            return render(
                request,
                "scan_new.html",
                roots=[s for s in accepted if not s.root.detected],
                home=[_disk_card(s.root) for s in accepted if s.root.personal],
                internal=[_disk_card(s.root) for s in accepted if s.root.internal],
                disks=[
                    _disk_card(s.root) for s in accepted if s.root.external and not s.root.internal
                ],
                refused_disks=_refused_disks(request, statuses),
                external_enabled=external_enabled,
                root=None,
            )
    status = statuses.get(root)
    if status is None or not status.accepted:
        raise HTTPException(404, "Ce dossier n'est pas autorisé.")
    try:
        parts = parse_subpath(path)
        listing = browse_root(request.app.state.config, status.root, path)
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


def _disk_card(root: RootConfig) -> dict[str, Any]:
    """An accepted disk or folder with its disk's usage (statvfs, metadata only)."""
    try:
        st = os.statvfs(root.path)
    except OSError:
        total = used = None
    else:
        total = st.f_blocks * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
    return {"root": root, "total": total, "used": used}


def _refused_disks(request: Request, statuses: dict[str, RootStatus]) -> list[dict[str, str]]:
    """Disks not offered, and why: refused on the host, or again in the container."""
    refused = [
        {"name": disk.name, "message": disk.message or ""}
        for disk in request.app.state.external_disks
        if not disk.accepted
    ]
    refused += [
        {"name": s.root.label, "message": REASON_MESSAGES[s.reason]}
        for s in statuses.values()
        if s.root.external and s.reason is not None
    ]
    return refused


@router.get("/archives")
def archives_page(request: Request, config: Config) -> Response:
    status = request.app.state.archive_status
    listing = None
    if status.enabled and config.archive_dir is not None:
        try:
            listing = list_archives(config.archive_dir)
        except OSError as exc:
            raise HTTPException(503, f"Dossier d'archives illisible : {exc.strerror}.") from exc
    return render(request, "archives.html", listing=listing)


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
    sort: explorer.Sort = explorer.Sort.SIZE,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
) -> Response:
    """The explorer (D-036): browse the analysed folder like a file manager."""
    scan = _scan_or_404(conn, scan_id)
    if dir is None:
        node = queries.start_node(conn, scan_id)
    else:
        node = queries.get_node(conn, scan_id, dir)
    if node is None or node.kind != "dir":
        raise HTTPException(404, "Ce dossier n'existe pas dans cette analyse.")
    nodes = explorer.sort_nodes(queries.all_children(conn, scan_id, node.entry_id), sort)
    pages = max(1, -(-len(nodes) // explorer.PAGE_SIZE))
    page = min(page, pages)
    shown = nodes[(page - 1) * explorer.PAGE_SIZE : page * explorer.PAGE_SIZE]
    source = explorer.MarkSource(conn, scan_id)
    marks = source.marks([(n.entry_id, n.raw_path, n.kind) for n in shown])
    return render(
        request,
        "space.html",
        scan=scan,
        title=analysis_title(config, scan),
        node=node,
        crumbs=queries.breadcrumb(conn, scan_id, node),
        children=shown,
        marks=marks,
        total_children=len(nodes),
        sort=sort,
        sorts=explorer.SORT_LABELS,
        page=page,
        pages=pages,
        dupes_run=source.dupes_run,
    )


@router.get("/scans/{scan_id}/entries/{entry_id}")
def entry_page(request: Request, scan_id: int, entry_id: int, conn: Db, config: Config) -> Response:
    """Everything TreeAudit knows about one file or folder."""
    scan = _scan_or_404(conn, scan_id)
    details = explorer.entry_details(conn, scan_id, entry_id)
    if details is None:
        raise HTTPException(404, "Cet élément n'existe pas dans cette analyse.")
    parent = (
        None if details.parent_id is None else queries.get_node(conn, scan_id, details.parent_id)
    )
    return render(
        request,
        "entry.html",
        scan=scan,
        title=analysis_title(config, scan),
        entry=details,
        prefix=path_prefix(config, scan),
        parent=parent,
        crumbs=queries.breadcrumb(conn, scan_id, parent) if parent is not None else [],
        dupes_run=explorer.latest_dupes_run(conn, scan_id),
    )


@router.get("/scans/{scan_id}/organize")
def organize_page(
    request: Request,
    scan_id: int,
    conn: Db,
    config: Config,
    dir: Annotated[int | None, Query(ge=1)] = None,
    tout: bool = False,
    suggerer: Annotated[str | None, Query(max_length=40)] = None,
) -> Response:
    """Tidying (D-041): the plan and a preview of the result; nothing is changed.

    Adjustments (elements left in place, other categories or folder names)
    come in the query string and are checked again before use."""
    scan = _scan_or_404(conn, scan_id)
    node = (
        queries.start_node(conn, scan_id) if dir is None else queries.get_node(conn, scan_id, dir)
    )
    if node is None or node.kind != "dir":
        raise HTTPException(404, "Ce dossier n'existe pas dans cette analyse.")
    plan, error = None, None
    try:
        adjustments = organize_plan.adjustments_from_query(request.query_params.multi_items())
        plan = organize_plan.make_plan(conn, scan_id, node.entry_id, adjustments)
    except organize_plan.AdjustmentError as exc:
        error = f"Ajustement refusé : {exc}"
    except organize_plan.PlanTooLargeError:
        error = (
            f"Ce dossier contient plus de {organize_plan.MAX_ENTRIES:,} éléments : "
            "choisis un sous-dossier dans l'explorateur.".replace(",", "\u202f")
        )
    folder = host_path(root_display(config, scan), node.rel_path)
    by_category: dict[str, list[Any]] = {}
    if plan is not None:
        for move in plan.moves:
            by_category.setdefault(move.category, []).append(move)
    llm: LlmClient | None = request.app.state.llm
    suggestions, suggestion_error = None, None
    if suggerer is not None and llm is not None and suggerer in by_category:
        # The model only proposes: shown next to the elements, applied only
        # when the user accepts them, then checked like any adjustment.
        try:
            suggestions = suggest_subfolders(llm, suggerer, by_category[suggerer])
        except LlmError as exc:
            suggestion_error = str(exc)
    return render(
        request,
        "organize.html",
        scan=scan,
        title=analysis_title(config, scan),
        node=node,
        crumbs=queries.breadcrumb(conn, scan_id, node),
        plan=plan,
        error=error,
        folder=folder,
        by_category=by_category,
        scripts_possible=safe_root(folder),
        shown_per_category=None if tout else 30,
        show_all=tout,
        categories=organize_plan.CATEGORIES,
        query=request.url.query,
        base_query="&".join(
            f"{quote(key)}={quote(value)}"
            for key, value in request.query_params.multi_items()
            if key != "suggerer"
        ),
        expert=llm is not None,
        suggestions=suggestions,
        suggestion_error=suggestion_error,
    )


@router.get("/scans/{scan_id}/basket")
def basket_page(request: Request, scan_id: int, conn: Db, config: Config) -> Response:
    """The basket. Its content lives in the browser; the list is fetched by app.js."""
    scan = _scan_or_404(conn, scan_id)
    return render(request, "basket.html", scan=scan, title=analysis_title(config, scan))


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
        prefix=path_prefix(config, scan),
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
