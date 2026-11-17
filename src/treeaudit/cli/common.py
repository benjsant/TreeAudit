"""What every command shares: exit codes, loading the configuration (and
entering the sandbox), opening the index, running a long job with Ctrl+C."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from treeaudit.cli.confinement import enter_sandbox
from treeaudit.core.config import (
    AppConfig,
    default_config_path,
    is_container_mode,
    load_config,
    native_defaults,
)
from treeaudit.core.datadir import prepare_data_dir
from treeaudit.roots.external import ExternalDisk
from treeaudit.roots.mountinfo import MountTable, parse_mountinfo, read_mountinfo
from treeaudit.roots.registry import current_home, with_external_disks, with_personal_folder
from treeaudit.roots.validation import REASON_MESSAGES, RootRejectedError

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_ERROR = 2
EXIT_INCOMPLETE = 3
EXIT_CANCELLED = 4

logger = logging.getLogger("treeaudit")


def exit_code(status: str) -> int:
    """The exit code of a finished scan, duplicate search or archive, by the
    value of its final state (D-012): the three share these words."""
    return {
        "complete": EXIT_OK,
        "incomplete": EXIT_INCOMPLETE,
        "cancelled": EXIT_CANCELLED,
    }.get(status, EXIT_ERROR)


def refused_root(exc: RootRejectedError) -> int:
    """Say why a root was refused when the work was about to start."""
    print(f"REFUSÉ [{exc.reason.value}] {exc.path} : {REASON_MESSAGES[exc.reason]}")
    return EXIT_REJECTED


def load_with_disks(args: argparse.Namespace) -> tuple[AppConfig, list[ExternalDisk]]:
    """Configuration, with the external disks detected at startup added as roots.

    In the native mode, the process enters the kernel sandbox right after: it
    may then write only in the data and archive folders (D-038).
    """
    path = args.config if args.config is not None else default_config_path()
    if args.config is None and not is_container_mode() and not path.exists():
        # No configuration file: the zero-configuration defaults (AppImage, D-038).
        config = native_defaults()
        prepare_data_dir(config.data_dir)
        if config.archive_dir is not None:
            prepare_data_dir(config.archive_dir)
    else:
        config = load_config(path, container_mode=is_container_mode())
    if getattr(args, "sandboxed", False):
        prepare_data_dir(config.data_dir)
        args.port = config.port
        archive = config.archive_dir
        enter_sandbox(
            args,
            write_dirs=(
                config.data_dir,
                *([archive] if archive is not None and os.path.isdir(archive) else []),
            ),
        )
    native_detection = config.external_disks and not config.container_mode
    config, disks = with_external_disks(
        config, read_mountinfo() if native_detection else None, os.environ
    )
    return with_personal_folder(config, os.environ, current_home()), disks


def load(args: argparse.Namespace) -> AppConfig:
    """The configuration with the detected disks (see ``load_with_disks``)."""
    return load_with_disks(args)[0]


def read_mounts(source: str | None) -> MountTable:
    """The mount table: this machine's, or one given as a file or on stdin ("-")."""
    if source is None:
        return read_mountinfo()
    if source == "-":
        return parse_mountinfo(sys.stdin.buffer.read())
    return parse_mountinfo(Path(source).read_bytes())


STATUS_LABELS = {
    "complete": "complet",
    "incomplete": "INCOMPLET",
    "cancelled": "ANNULÉ",
    "failed": "ÉCHEC",
    "running": "en cours",
    "pending": "en attente",
}


def open_index(args: argparse.Namespace) -> sqlite3.Connection:
    """Open the index, and mark the scans a crash left running as failed (D-012)."""
    from treeaudit.core.datadir import ScanLock, ScanLockedError
    from treeaudit.db import dupes, scans
    from treeaudit.db.connection import connect

    config = load(args)
    prepare_data_dir(config.data_dir)
    conn = connect(config.data_dir)
    # Scans left "running" by a crash are only stale if no scan holds the lock.
    lock = ScanLock(config.data_dir)
    try:
        lock.acquire()
    except ScanLockedError:
        return conn
    try:
        scans.fail_interrupted_scans(conn)
        dupes.fail_interrupted_runs(conn)
    finally:
        lock.release()
    return conn


def run_in_thread[R](
    job: Callable[[threading.Event], R],
    describe: Callable[[], str],
    *,
    name: str = "treeaudit-job",
) -> tuple[R | None, BaseException | None]:
    """Run a long job in a thread, show its progress (``describe``) every half
    second, and cancel it on Ctrl+C. Returns its result or its failure, which
    the caller reports: the thread itself never prints an error."""
    cancel = threading.Event()
    result: list[R] = []
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(job(cancel))
        except BaseException as exc:  # reported by the caller
            failure.append(exc)

    thread = threading.Thread(target=worker, name=name)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
            print(f"\r{describe()}", end="", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nannulation demandée…", file=sys.stderr)
        cancel.set()
        thread.join()
    print(file=sys.stderr)
    return (result[0] if result else None), (failure[0] if failure else None)
