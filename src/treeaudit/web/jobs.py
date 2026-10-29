"""Background jobs for the web interface: one scan, duplicate analysis or archive
at a time.

Jobs reuse the engine entry points (execute_scan, execute_duplicates,
execute_archive), which take the data-directory locks: a job started while the
CLI is running the same kind of work fails cleanly instead of running
concurrently.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl

from treeaudit.archive.destination import ARCHIVE_DIR_MESSAGES, ArchiveDirRejectedError
from treeaudit.archive.runner import (
    ArchiveOutcome,
    ArchiveProgress,
    ArchiveRequest,
    InsufficientSpaceError,
    execute_archive,
)
from treeaudit.core.config import AppConfig
from treeaudit.core.datadir import ArchiveLockedError, ScanLock, ScanLockedError, index_size
from treeaudit.core.errors import TreeAuditError
from treeaudit.db import scans as scans_db
from treeaudit.db.connection import connect
from treeaudit.duplicates.runner import DupOptions, DupOutcome, DupProgress, execute_duplicates
from treeaudit.llm.client import LlmClient, LlmError
from treeaudit.llm.facts import facts_for_finding
from treeaudit.llm.opinion import Opinion, ask_opinion
from treeaudit.llm.organize import SuggestionsCancelled, SuggestProgress, suggest
from treeaudit.organize.plan import PlanTooLargeError, adjustments_from_query, make_plan
from treeaudit.organize.subfolders import Suggestions
from treeaudit.parasites.cached import parasite_findings
from treeaudit.parasites.detect import Band
from treeaudit.parasites.report import human_size
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import REASON_MESSAGES, RootRejectedError
from treeaudit.scanner.models import ScanStatus
from treeaudit.scanner.runner import ScanOutcome, ScanProgress, ScanRequest, execute_scan
from treeaudit.scanner.walker import parse_subpath
from treeaudit.web import summaries

logger = logging.getLogger(__name__)


class JobKind(StrEnum):
    SCAN = "scan"
    DUPES = "dupes"
    ARCHIVE = "archive"
    SUGGEST = "suggest"
    OPINIONS = "opinions"
    COMPACT = "compact"


@dataclass(frozen=True, slots=True)
class CompactOutcome:
    """The index gave its free space back to the disk (D-043)."""

    status: ScanStatus
    before: int
    after: int
    result_url: str = "/?compacte="


MAX_OPINIONS = 50
"""Folders "to check" or "doubtful" given to the AI per request."""


@dataclass(frozen=True, slots=True)
class OpinionsOutcome:
    """The local AI's opinions on the folders to check (D-040). Shown next to
    them only: they never check, uncheck nor reclassify anything."""

    status: ScanStatus
    scan_id: int
    min_confidence: int
    opinions: dict[int, Opinion] = field(default_factory=dict)
    """Finding number -> opinion."""
    failed: int = 0
    considered: int = 0
    total: int = 0

    @property
    def result_url(self) -> str:
        return f"/scans/{self.scan_id}/parasites?min_confidence={self.min_confidence}&avis="


@dataclass(frozen=True, slots=True)
class SuggestOutcome:
    """Sub-folders suggested by the local AI for one category (D-041). Shown on
    the tidying page only: nothing applies until the user accepts it."""

    status: ScanStatus
    scan_id: int
    dir_id: int
    category: str
    query: str
    """The page's adjustments, to come back to the same preview."""
    suggestions: Suggestions | None = None

    @property
    def result_url(self) -> str:
        query = f"{self.query}&" if self.query else ""
        return f"/scans/{self.scan_id}/organize?{query}suggestions="


class JobError(TreeAuditError):
    """The job cannot be started; the message is shown to the user."""


Outcome = (
    ScanOutcome | DupOutcome | ArchiveOutcome | SuggestOutcome | OpinionsOutcome | CompactOutcome
)


@dataclass(slots=True)
class Job:
    id: int
    kind: JobKind
    label: str
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: ScanProgress | DupProgress | ArchiveProgress | SuggestProgress = field(
        default_factory=ScanProgress
    )
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    outcome: Outcome | None = None
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
        elif isinstance(progress, SuggestProgress):
            data["progress"] = {
                "stage": progress.stage,
                "done": progress.done,
                "total": progress.total,
            }
        elif isinstance(progress, ArchiveProgress):
            data["progress"] = {
                "stage": progress.stage.value,
                "entries_total": progress.entries_total,
                "entries_done": progress.entries_done,
                "bytes_total": progress.bytes_total,
                "bytes_read": progress.bytes_read,
                "bytes_verified": progress.bytes_verified,
                "problems": progress.problems,
            }
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
        if isinstance(self.outcome, ArchiveOutcome):
            data["archive"] = _archive_result(self.outcome)
        if isinstance(self.outcome, SuggestOutcome | OpinionsOutcome | CompactOutcome):
            data["result_url"] = self.outcome.result_url + str(self.id)
        return data


def _archive_result(outcome: ArchiveOutcome) -> dict[str, Any]:
    """Archive outcome for the page. The path is the one the user chose (display only)."""
    return {
        "path": outcome.archive_path_shown,
        "verified": outcome.verified,
        "same_disk": outcome.same_disk,
        "entries": outcome.entries,
        "bytes_read": outcome.bytes_read,
        "archive_size": outcome.archive_size,
        "failure": outcome.failure,
        "problem_count": outcome.problem_count,
        "problems": [{"path": p.path, "message": p.message} for p in outcome.problems],
    }


def _error_message(exc: TreeAuditError) -> str:
    """Plain-French message for an error raised before a job could do anything."""
    if isinstance(exc, ArchiveDirRejectedError):
        return f"archivage impossible : {ARCHIVE_DIR_MESSAGES[exc.reason]}"
    if isinstance(exc, InsufficientSpaceError):
        return (
            f"espace insuffisant dans le dossier d'archives : {human_size(exc.needed)} "
            f"nécessaires au pire, {human_size(exc.available)} disponibles. Rien n'a été écrit."
        )
    if isinstance(exc, ArchiveLockedError):
        return "un archivage est déjà en cours (peut-être en ligne de commande)"
    if isinstance(exc, ScanLockedError):
        return "une analyse est déjà en cours (peut-être en ligne de commande)"
    return str(exc)


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

    def _start(
        self,
        job: Job,
        work: Callable[[Job], Outcome],
    ) -> Job:
        with self._lock:
            if self.current is not None and self.current.running:
                raise JobError("une tâche est déjà en cours ; attends sa fin ou annule-la")
            self.current = job

        def run() -> None:
            try:
                job.outcome = work(job)
            except RootRejectedError as exc:
                job.error = f"racine refusée : {REASON_MESSAGES[exc.reason]}"
            except TreeAuditError as exc:
                job.error = _error_message(exc)
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
        shown = root.shown_path.rstrip("/") + (f"/{normalized}" if normalized else "")
        label = f"Analyse de « {os.path.basename(shown) or root.label} »"
        progress = ScanProgress()
        job = Job(id=next(self._ids), kind=JobKind.SCAN, label=label, progress=progress)
        request = ScanRequest(root=root, subpath=normalized)

        def work(j: Job) -> ScanOutcome:
            outcome = execute_scan(
                self.config,
                request,
                mounts=self._mounts(),
                home=self._home(),
                cancel=j.cancel,
                progress=progress,
            )
            self._warm_summary(outcome.scan_id)
            return outcome

        return self._start(job, work)

    def _warm_summary(self, scan_id: int) -> None:
        """Compute the summary page data now, so the summary opens instantly."""
        conn = connect(self.config.data_dir)
        try:
            summaries.parasites_summary(conn, scan_id)
        finally:
            conn.close()

    def start_dupes(self, scan_id: int, *, micro: bool) -> Job:
        options = DupOptions.for_micro() if micro else DupOptions()
        label = "Recherche des petits fichiers en double" if micro else "Recherche des doublons"
        progress = DupProgress()
        job = Job(id=next(self._ids), kind=JobKind.DUPES, label=label, progress=progress)

        def work(j: Job) -> DupOutcome:
            outcome = execute_duplicates(
                self.config,
                scan_id,
                options,
                mounts=self._mounts(),
                home=self._home(),
                cancel=j.cancel,
                progress=progress,
            )
            conn = connect(self.config.data_dir)
            try:
                summaries.dupes_summary(conn, outcome.run_id)
            finally:
                conn.close()
            return outcome

        return self._start(job, work)

    def start_suggestions(
        self, llm: LlmClient, scan_id: int, dir_id: int, category: str, query: str
    ) -> Job:
        """Sub-folder suggestions of the local AI for one category of a tidying
        plan (D-041). The model only proposes: the result is shown, never applied."""
        progress = SuggestProgress()
        label = f"Suggestions de l'IA pour « {category} »"
        job = Job(id=next(self._ids), kind=JobKind.SUGGEST, label=label, progress=progress)
        adjustments = adjustments_from_query(parse_qsl(query, keep_blank_values=True))

        def work(j: Job) -> SuggestOutcome:
            conn = connect(self.config.data_dir)
            try:
                plan = make_plan(conn, scan_id, dir_id, adjustments)
            except PlanTooLargeError as exc:
                raise JobError("dossier trop grand : choisis un sous-dossier") from exc
            finally:
                conn.close()
            moves = [move for move in plan.moves if move.category == category]
            try:
                found = suggest(llm, category, moves, progress=progress, cancel=j.cancel)
            except SuggestionsCancelled:
                return SuggestOutcome(ScanStatus.CANCELLED, scan_id, dir_id, category, query)
            return SuggestOutcome(ScanStatus.COMPLETE, scan_id, dir_id, category, query, found)

        return self._start(job, work)

    def start_opinions(self, llm: LlmClient, scan_id: int, min_confidence: int) -> Job:
        """The local AI's opinion on each folder "to check" or "doubtful" (D-040)."""
        progress = SuggestProgress(stage="avis")
        label = "Avis de l'IA sur les dossiers à vérifier"
        job = Job(id=next(self._ids), kind=JobKind.OPINIONS, label=label, progress=progress)

        def work(j: Job) -> OpinionsOutcome:
            conn = connect(self.config.data_dir)
            try:
                findings = [
                    f
                    for f in parasite_findings(conn, scan_id, min_confidence=min_confidence)
                    if f.band is not Band.GREEN
                ]
                chosen = findings[:MAX_OPINIONS]
                facts = [(f.number, facts_for_finding(conn, scan_id, f)) for f in chosen]
            finally:
                conn.close()
            progress.total = len(facts)
            opinions: dict[int, Opinion] = {}
            failed = 0
            for number, folder in facts:
                if j.cancel.is_set():
                    return OpinionsOutcome(
                        ScanStatus.CANCELLED, scan_id, min_confidence, opinions, failed
                    )
                try:
                    opinions[number] = ask_opinion(llm, folder)[0]
                except LlmError:
                    failed += 1  # one bad answer does not stop the others
                progress.done += 1
            return OpinionsOutcome(
                ScanStatus.COMPLETE if not failed else ScanStatus.INCOMPLETE,
                scan_id,
                min_confidence,
                opinions,
                failed,
                len(facts),
                len(findings),
            )

        return self._start(job, work)

    def start_compact(self) -> Job:
        """VACUUM the index: only TreeAudit's own data, after analyses were forgotten."""
        progress = SuggestProgress(stage="compactage")
        job = Job(
            id=next(self._ids),
            kind=JobKind.COMPACT,
            label="Compactage de l'index",
            progress=progress,
        )

        def work(j: Job) -> CompactOutcome:
            before = index_size(self.config.data_dir)
            conn = connect(self.config.data_dir)
            try:
                with ScanLock(self.config.data_dir):  # never during a scan, even from the CLI
                    scans_db.compact(conn)
            finally:
                conn.close()
            return CompactOutcome(ScanStatus.COMPLETE, before, index_size(self.config.data_dir))

        return self._start(job, work)

    def start_archive(
        self, root_id: str, subpath: str, *, expected_folder: tuple[int, int] | None = None
    ) -> Job:
        """Archive a folder of a root; ``subpath`` may hold undecodable bytes
        (surrogate escapes) when it comes from the index, together with the
        folder identity recorded there (``expected_folder``)."""
        root = self.config.get_root(root_id)
        if root is None:
            raise JobError("racine inconnue")
        parts = parse_subpath(subpath)  # SubpathError if unsafe
        name = parts[-1] if parts else os.path.basename(root.shown_path.rstrip("/"))
        shown = os.fsencode(name).decode("utf-8", errors="replace") or root.label
        label = f"Archive de « {shown} »"
        progress = ArchiveProgress()
        job = Job(id=next(self._ids), kind=JobKind.ARCHIVE, label=label, progress=progress)
        request = ArchiveRequest(
            root=root, subpath="/".join(parts), expected_folder=expected_folder
        )

        def work(j: Job) -> ArchiveOutcome:
            return execute_archive(
                self.config,
                request,
                mounts=self._mounts(),
                home=self._home(),
                cancel=j.cancel,
                progress=progress,
            )

        return self._start(job, work)

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
