"""FastAPI application factory.

Routes identify roots by their configured id only: no route accepts a filesystem
path, and no response contains a host path (docs/SECURITY.md).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from treeaudit import __version__
from treeaudit.core.config import ROOT_ID_PATTERN
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import REASON_MESSAGES
from treeaudit.web.security import install_security_middlewares

RootId = Annotated[str, PathParam(pattern=f"^{ROOT_ID_PATTERN.pattern}$")]


def _root_to_json(status: RootStatus) -> dict[str, Any]:
    return {
        "id": status.root.id,
        "label": status.root.label,
        "accepted": status.accepted,
        "reason": None if status.reason is None else status.reason.value,
        "message": None if status.reason is None else REASON_MESSAGES[status.reason],
    }


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # The default handler echoes the rejected input back; keep responses free of it.
    return JSONResponse({"detail": "invalid request parameters"}, status_code=422)


def create_app(statuses: tuple[RootStatus, ...]) -> FastAPI:
    """Build the application for the given, already validated, roots."""
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

    by_id = {status.root.id: status for status in statuses}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/roots")
    def list_roots() -> dict[str, list[dict[str, Any]]]:
        return {"roots": [_root_to_json(status) for status in statuses]}

    @app.get("/api/roots/{root_id}")
    def get_root(root_id: RootId) -> dict[str, Any]:
        status = by_id.get(root_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown root")
        return _root_to_json(status)

    return app
