"""``archive``: verified archives (D-024, D-032). The original is never modified."""

from __future__ import annotations

import argparse
import sys

from treeaudit.archive.destination import (
    ARCHIVE_DIR_MESSAGES,
    ArchiveDirContext,
    ArchiveDirReason,
    ArchiveDirRejectedError,
    evaluate_archive_dir,
    validate_archive_dir,
)
from treeaudit.cli.common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REJECTED,
    exit_code,
    load,
    read_mounts,
    refused_root,
    run_in_thread,
)
from treeaudit.core.sizes import human_size
from treeaudit.roots.mountinfo import read_mountinfo
from treeaudit.roots.registry import current_home
from treeaudit.roots.validation import RootRejectedError


def archive_check(args: argparse.Namespace) -> int:
    """Tell whether a folder may receive archives (run by ./treeaudit on the host)."""
    ctx = ArchiveDirContext(
        mounts=read_mounts(args.mountinfo),
        home=args.home if args.home is not None else current_home(),
        data_dir=args.data_dir,
        root_paths=tuple(args.root),
        check_filesystem=not args.no_fs_checks,
    )
    try:
        validate_archive_dir(args.path, ctx)
    except ArchiveDirRejectedError as exc:
        print(f"REFUSÉ [{exc.reason.value}] {args.path} : {ARCHIVE_DIR_MESSAGES[exc.reason]}")
        return EXIT_REJECTED
    print(f"OK {args.path} peut recevoir des archives")
    if args.data_dir is None or not args.root:
        print("  (chevauchement non vérifié : --data-dir ou --root absent)")
    if args.no_fs_checks:
        print("  (existence, type, liens et droits non vérifiés : --no-fs-checks)")
    return EXIT_OK


def archive_status(args: argparse.Namespace) -> int:
    """The state of the configured archive folder: enabled, or why not."""
    config = load(args)
    status = evaluate_archive_dir(config, read_mountinfo(), current_home())
    if status.enabled:
        print(f"Archivage activé : {status.shown_path}")
    else:
        shown = status.shown_path and status.reason is not ArchiveDirReason.NOT_MOUNTED
        where = f" ({status.shown_path})" if shown else ""
        print(f"Archivage désactivé{where} : {status.message}")
    return EXIT_OK


_ARCHIVE_STAGES = {
    "preparing": "préparation",
    "writing": "écriture",
    "verifying": "vérification",
    "done": "terminé",
}


def archive_run(args: argparse.Namespace) -> int:
    """Write a verified archive of a folder; the original is never modified."""
    from treeaudit.archive.output import Compression
    from treeaudit.archive.runner import (
        ArchiveProgress,
        ArchiveRequest,
        ArchiveStatus,
        InsufficientSpaceError,
        execute_archive,
    )

    config = load(args)
    root = config.get_root(args.root_id)
    if root is None:
        print(f"erreur : racine inconnue : {args.root_id}", file=sys.stderr)
        return EXIT_ERROR
    request = ArchiveRequest(
        root=root,
        subpath=args.path,
        compression=Compression.XZ if args.xz else Compression.GZ,
    )
    progress = ArchiveProgress()

    def describe() -> str:
        if progress.stage == "verifying":
            done = f"{human_size(progress.bytes_verified)} relus"
        else:
            done = (
                f"{progress.entries_done}/{progress.entries_total} éléments, "
                f"{human_size(progress.bytes_read)}/{human_size(progress.bytes_total)}"
            )
        return f"{_ARCHIVE_STAGES[progress.stage]} : {done}, {progress.problems} problème(s)    "

    outcome, failure = run_in_thread(
        lambda cancel: execute_archive(
            config,
            request,
            mounts=read_mountinfo(),
            home=current_home(),
            cancel=cancel,
            progress=progress,
        ),
        describe,
    )
    if failure is not None:
        if isinstance(failure, ArchiveDirRejectedError):
            print(f"REFUSÉ [{failure.reason.value}] {ARCHIVE_DIR_MESSAGES[failure.reason]}")
            return EXIT_REJECTED
        if isinstance(failure, RootRejectedError):
            return refused_root(failure)
        if isinstance(failure, InsufficientSpaceError):
            print(
                f"REFUSÉ : espace insuffisant dans le dossier d'archives "
                f"({human_size(failure.needed)} nécessaires au pire, "
                f"{human_size(failure.available)} disponibles). Rien n'a été écrit."
            )
            return EXIT_REJECTED
        raise failure
    if outcome is None:  # pragma: no cover - the worker either returns or fails
        return EXIT_ERROR

    status = outcome.status
    if status is ArchiveStatus.COMPLETE:
        print(f"✔ Archive complète et vérifiée : {outcome.archive_path_shown}")
    elif status is ArchiveStatus.INCOMPLETE:
        print(
            f"⚠ Archive INCOMPLÈTE (vérifiée, mais il manque des éléments) : "
            f"{outcome.archive_path_shown}"
        )
        print("  Ne pas supprimer l'original en se fiant à cette archive.")
    elif status is ArchiveStatus.CANCELLED:
        print("Archivage annulé : le fichier temporaire a été retiré, rien n'a été conservé.")
    else:
        print("✘ Archivage échoué : le fichier temporaire a été retiré, rien n'a été conservé.")
    if outcome.failure:
        print(f"cause : {outcome.failure}")
    if outcome.verified:
        print(
            f"  {outcome.entries} éléments, {human_size(outcome.bytes_read)} lus, "
            f"archive de {human_size(outcome.archive_size)}"
        )
    if outcome.same_disk and outcome.archive_name:
        print(
            "⚠ L'archive est sur le même disque que l'original : ce n'est pas une vraie "
            "sauvegarde (une panne du disque ferait perdre les deux)."
        )
    if outcome.problem_count:
        print(f"\nProblèmes ({outcome.problem_count}) :")
        for problem in outcome.problems:
            print(f"  {problem.path} : {problem.message}")
        hidden = outcome.problem_count - len(outcome.problems)
        if hidden > 0:
            print(f"  … et {hidden} autres")
    return exit_code(status.value)
