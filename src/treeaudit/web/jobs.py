"""Background jobs for the web interface: one scan or duplicate analysis at a time.

Jobs reuse the engine entry points (execute_scan, execute_duplicates), which
take the data-directory lock: a job started while the CLI is running a scan
fails cleanly instead of running concurrently.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from treeaudit.core.config import AppConfig
from treeaudit.core.errors import TreeAuditError
from treeaudit.duplicates.runner import DupOptions, DupOutcome, DupProgress, execute_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import REASON_MESSAGES, RootRejectedError
from treeaudit.scanner.runner import ScanOutcome, ScanProgress, ScanRequest, execute_scan
from treeaudit.scanner.walker import parse_subpath

logger = logging.getLogger(__name__)


class JobKind(StrEnum):
    SCAN = "scan"
    DUPES = "dupes"


class JobError(TreeAuditError):
    """The job cannot be started; the message is shown to the user."""


@dataclass(slots=True)
class Job:
    id: int
    kind: JobKind
    label: str
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: ScanProgress | DupProgress = field(default_factory=ScanProgress)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    outcome: ScanOutcome | DupOutcome | None = None
    error: str | None = None
    thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def snapshot(self) -> dict[str, Any]:
        """JSON-friendly state for the API (no host path)."""
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
            "label": self.label,
            "running": self.running,
            "cancel_requested": self.cancel.is_set(),
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "error": self.error,
        }
        progress = self.progress
        if isinstance(progress, ScanProgress):
            data["progress"] = {
                "entries": progress.entries,
                "errors": progress.errors,
                "exclusions": progress.exclusions,
                "current_dir": progress.current_dir,
            }
            data["scan_id"] = progress.scan_id
        else:
            data["progress"] = {
                "stage": progress.stage,
                "candidates": progress.candidates,
                "files_hashed": progress.files_hashed,
                "bytes_read": progress.bytes_read,
                "errors": progress.errors,
            }
            data["run_id"] = progress.run_id
        if self.outcome is not None:
            data["status"] = self.outcome.status.value
        return data


class JobManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        mounts: Callable[[], MountTable],
        home: Callable[[], str | None],
    ) -> None:
        self.config = config
        self._mounts = mounts
        self._home = home
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.current: Job | None = None

    def _start(self, job: Job, work: Callable[[Job], ScanOutcome | DupOutcome]) -> Job:
        with self._lock:
            if self.current is not None and self.current.running:
                raise JobError("une analyse est déjà en cours ; attends sa fin ou annule-la")
            self.current = job

        def run() -> None:
            try:
                job.outcome = work(job)
            except RootRejectedError as exc:
                job.error = f"racine refusée : {REASON_MESSAGES[exc.reason]}"
            except TreeAuditError as exc:
                job.error = str(exc)
            except Exception as exc:  # reported to the user, logged with traceback
                logger.exception("job %d failed", job.id)
                job.error = f"erreur interne : {type(exc).__name__}"
            finally:
                job.finished_at = time.time()

        job.thread = threading.Thread(target=run, name=f"treeaudit-job-{job.id}", daemon=True)
        job.thread.start()
        return job

    def start_scan(self, root_id: str, subpath: str) -> Job:
        root = self.config.get_root(root_id)
        if root is None:
            raise JobError("racine inconnue")
        normalized = "/".join(parse_subpath(subpath))  # SubpathError if unsafe
        label = f"scan de {root.label}" + (f" / {normalized}" if normalized else "")
        progress = ScanProgress()
        job = Job(id=next(self._ids), kind=JobKind.SCAN, label=label, progress=progress)
        request = ScanRequest(root=root, subpath=normalized)
        return self._start(
            job,
            lambda j: execute_scan(
                self.config,
                request,
                mounts=self._mounts(),
                home=self._home(),
                cancel=j.cancel,
                progress=progress,
            ),
        )

    def start_dupes(self, scan_id: int, *, micro: bool) -> Job:
        options = DupOptions.for_micro() if micro else DupOptions()
        label = f"{'micro-nettoyage' if micro else 'doublons'} du scan {scan_id}"
        progress = DupProgress()
        job = Job(id=next(self._ids), kind=JobKind.DUPES, label=label, progress=progress)
        return self._start(
            job,
            lambda j: execute_duplicates(
                self.config,
                scan_id,
                options,
                mounts=self._mounts(),
                home=self._home(),
                cancel=j.cancel,
                progress=progress,
            ),
        )

    def cancel(self) -> bool:
        job = self.current
        if job is None or not job.running:
            return False
        job.cancel.set()
        return True

    def wait(self, timeout: float = 30.0) -> Job | None:
        """Wait for the current job (tests and shutdown)."""
        job = self.current
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return job
