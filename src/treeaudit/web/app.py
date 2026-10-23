"""FastAPI application factory.

Routes identify roots by their configured id, scans and analyses by their
number, folders by a path relative to a root: no route accepts an absolute
path, and no response contains a host path except display paths: the one used
in the proposed commands (D-025) and the archive directory the user chose (D-032).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from treeaudit import __version__
from treeaudit.archive.destination import evaluate_archive_dir
from treeaudit.core.config import ROOT_ID_PATTERN, AppConfig
from treeaudit.core.datadir import prepare_data_dir
from treeaudit.llm.client import LlmClient, client_from_environment
from treeaudit.roots.external import ExternalDisk
from treeaudit.roots.mountinfo import MountTable, read_mountinfo
from treeaudit.roots.registry import RootStatus, current_home
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.web import api, exports, pages
from treeaudit.web.jobs import JobManager
from treeaudit.web.live import Detection
from treeaudit.web.security import install_security_middlewares

RootId = Annotated[str, PathParam(pattern=f"^{ROOT_ID_PATTERN.pattern}$")]
STATIC_DIR = Path(__file__).parent / "static"


def _root_to_json(status: RootStatus) -> dict[str, Any]:
    return {
        "id": status.root.id,
        "label": status.root.label,
        "accepted": status.accepted,
        "reason": None if status.reason is None else status.reason.value,
        "message": None if status.reason is None else REASON_MESSAGES[status.reason],
    }


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/export/", "/health"))


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    # The default handler echoes the rejected input back; keep responses free of it.
    if _wants_json(request):
        return JSONResponse({"detail": "invalid request parameters"}, status_code=422)
    return pages.error_page(request, 422, "Paramètres invalides.")


async def _http_error_handler(request: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = exc.detail if isinstance(exc, StarletteHTTPException) else "erreur"
    if _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=status)
    return pages.error_page(request, status, str(detail))


def create_app(
    config: AppConfig,
    statuses: tuple[RootStatus, ...],
    *,
    mounts: Callable[[], MountTable] = read_mountinfo,
    home: Callable[[], str | None] = current_home,
    external_disks: Sequence[ExternalDisk] = (),
    external_enabled: bool = False,
    quit: Callable[[], None] | None = None,
    detect: Detection | None = None,
    llm: LlmClient | None = None,
) -> FastAPI:
    """Build the application for the given, already validated, roots.

    ``quit`` stops the server: offered as a button in the native mode, which
    may run without a terminal (AppImage, D-038). ``detect`` finds the disks
    plugged in now (native mode: the host's mounts are seen live); the roots
    are then refreshed when the home and choice pages open.
    """
    prepare_data_dir(config.data_dir)
    app = FastAPI(
        title="TreeAudit",
        version=__version__,
        # Interactive docs load assets from a CDN and widen the attack surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_security_middlewares(app)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)

    app.state.config = config
    app.state.statuses = {status.root.id: status for status in statuses}
    # Every detected disk, refused ones included, to explain the choices (D-033).
    app.state.external_disks = tuple(external_disks)
    app.state.external_enabled = external_enabled or bool(external_disks)
    app.state.jobs = JobManager(config, mounts=mounts, home=home)
    app.state.quit = quit
    app.state.detect = detect
    # Expert mode (D-040): the local LLM, which only ever proposes.
    app.state.llm = llm if llm is not None else client_from_environment()
    app.state.detect_lock = threading.Lock()
    # Shown on the pages; execute_archive validates the directory again before writing.
    app.state.archive_status = (
        evaluate_archive_dir(config, mounts(), home())
        if config.archive_dir is not None
        else evaluate_archive_dir(config, MountTable(entries=()), None)
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/roots")
    def list_roots() -> dict[str, list[dict[str, Any]]]:
        return {"roots": [_root_to_json(status) for status in statuses]}

    @app.get("/api/roots/{root_id}")
    def get_root(root_id: RootId) -> dict[str, Any]:
        status = app.state.statuses.get(root_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown root")
        return _root_to_json(status)

    app.include_router(api.router)
    app.include_router(exports.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
