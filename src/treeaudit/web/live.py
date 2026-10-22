"""Roots seen live in the native mode (D-038): disks plugged in or removed
while TreeAudit runs.

In Docker the container only sees what was mounted at its start; natively the
process sees the machine's mounts as they are now. The same detection and
validation as at startup run again (D-033, D-037): only what is offered
changes, and each root is validated once more right before any scan or archive.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastapi import FastAPI

from treeaudit.core.config import AppConfig
from treeaudit.roots.external import ExternalDisk
from treeaudit.roots.registry import RootStatus

# The roots as found now: the configuration with its roots, their validation,
# and every detected disk, refused ones included.
Detection = Callable[[], tuple[AppConfig, tuple[RootStatus, ...], Sequence[ExternalDisk]]]


def refresh_roots(app: FastAPI) -> None:
    """Take the disks plugged in, or removed, since the last look."""
    detect: Detection | None = app.state.detect
    if detect is None:
        return
    with app.state.detect_lock:
        config, statuses, disks = detect()
        app.state.config = config
        app.state.statuses = {status.root.id: status for status in statuses}
        app.state.external_disks = tuple(disks)
        app.state.jobs.config = config
