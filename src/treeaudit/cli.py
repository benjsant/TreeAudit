"""Command-line entry point.

Exit codes: 0 success, 1 root rejected, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from treeaudit import __version__
from treeaudit.archive.destination import (
    ARCHIVE_DIR_MESSAGES,
    ArchiveDirContext,
    ArchiveDirReason,
    ArchiveDirRejectedError,
    evaluate_archive_dir,
    validate_archive_dir,
)
from treeaudit.core.config import (
    CONTAINER_BIND_HOST,
    DEFAULT_PORT,
    AppConfig,
    default_config_path,
    is_container_mode,
    load_config,
    native_defaults,
)
from treeaudit.core.datadir import prepare_data_dir
from treeaudit.core.errors import TreeAuditError
from treeaudit.core.sandbox import Policy, SandboxError, restrict
from treeaudit.filters.spec import FilterSpec
from treeaudit.parasites.commands import Action
from treeaudit.parasites.detect import DEFAULT_MIN_CONFIDENCE
from treeaudit.parasites.report import human_size
from treeaudit.roots.external import (
    DISKS_ENV_VAR,
    ExternalDisk,
    compose_override,
    default_media_dirs,
    find_external_disks,
    format_device_specs,
    system_device_specs,
    system_disks,
    system_partitions,
)
from treeaudit.roots.mountinfo import MountInfoError, MountTable, parse_mountinfo, read_mountinfo
from treeaudit.roots.registry import (
    RootStatus,
    current_home,
    evaluate_roots,
    with_external_disks,
    with_personal_folder,
)
from treeaudit.roots.validation import (
    FORBIDDEN_ROOTS,
    REASON_MESSAGES,
    RootRejectedError,
    ValidationContext,
    is_same_or_parent,
    is_within,
    validate_root,
)
from treeaudit.scanner.walker import DEFAULT_MAX_DEPTH, SubpathError

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_ERROR = 2
EXIT_INCOMPLETE = 3
EXIT_CANCELLED = 4

logger = logging.getLogger("treeaudit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="treeaudit", description="Audit local et en lecture seule de fichiers et dossiers."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="fichier de configuration (défaut : $TREEAUDIT_CONFIG ou "
        "~/.config/treeaudit/config.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="journalisation détaillée")
    commands = parser.add_subparsers(dest="command", required=True)

    roots = commands.add_parser("roots", help="racines d'audit")
    roots_commands = roots.add_subparsers(dest="roots_command", required=True)

    roots_commands.add_parser("list", help="lister les racines configurées et leur état")

    check = roots_commands.add_parser("check", help="vérifier qu'un chemin peut être audité")
    check.add_argument("path", help="chemin absolu à vérifier")
    check.add_argument(
        "--allow-system-volume",
        action="store_true",
        help="accepter un chemin situé sur le volume du système",
    )
    check.add_argument(
        "--mountinfo",
        default=None,
        help="fichier mountinfo à utiliser ('-' pour l'entrée standard) ; "
        "défaut : /proc/self/mountinfo",
    )
    check.add_argument("--home", default=None, help="dossier personnel à protéger")
    check.add_argument(
        "--data-dir", default=None, help="dossier de données TreeAudit à ne pas chevaucher"
    )
    check.add_argument(
        "--personal",
        action="store_true",
        help="le chemin est le dossier personnel, proposé en entier (D-037) : il peut être sur "
        "le volume du système et contenir le dossier de données, écarté pendant l'analyse",
    )
    check.add_argument(
        "--no-fs-checks",
        action="store_true",
        help="ne pas accéder au chemin (existence, type, liens) : à utiliser seulement quand "
        "ces vérifications ont déjà été faites sur la machine qui possède le chemin",
    )

    archive = commands.add_parser("archive", help="archives vérifiées (D-032)")
    archive_commands = archive.add_subparsers(dest="archive_command", required=True)
    archive_commands.add_parser("status", help="état du dossier d'archives configuré")
    archive_run = archive_commands.add_parser(
        "run",
        help="créer une archive vérifiée d'un dossier (l'original n'est jamais modifié ; "
        "Ctrl+C pour annuler)",
    )
    archive_run.add_argument("root_id", help="identifiant de la racine configurée")
    archive_run.add_argument(
        "--path", default="", help="sous-dossier à archiver, relatif à la racine (défaut : tout)"
    )
    archive_run.add_argument(
        "--xz", action="store_true", help="format .tar.xz (plus compact, beaucoup plus lent)"
    )
    archive_check = archive_commands.add_parser(
        "check", help="vérifier qu'un dossier peut recevoir des archives"
    )
    archive_check.add_argument("path", help="chemin absolu à vérifier")
    archive_check.add_argument(
        "--mountinfo",
        default=None,
        help="fichier mountinfo à utiliser ('-' pour l'entrée standard) ; "
        "défaut : /proc/self/mountinfo",
    )
    archive_check.add_argument("--home", default=None, help="dossier personnel à protéger")
    archive_check.add_argument(
        "--data-dir", default=None, help="dossier de données TreeAudit à ne pas chevaucher"
    )
    archive_check.add_argument(
        "--root",
        action="append",
        default=[],
        help="dossier audité à ne pas chevaucher (option répétable)",
    )
    archive_check.add_argument(
        "--no-fs-checks",
        action="store_true",
        help="ne pas accéder au chemin (existence, type, liens, droits) : à utiliser seulement "
        "quand ces vérifications ont déjà été faites sur la machine qui possède le chemin",
    )

    disks = commands.add_parser("disks", help="disques externes (D-033)")
    disks_commands = disks.add_subparsers(dest="disks_command", required=True)
    disks_commands.add_parser("list", help="disques externes proposés ou refusés, et pourquoi")
    detect = disks_commands.add_parser(
        "detect",
        help="détecter les disques externes de l'hôte et écrire le fichier Compose qui les "
        "monte en lecture seule (utilisé par ./treeaudit)",
    )
    detect.add_argument(
        "--mountinfo", default=None, help="mountinfo de l'hôte ('-' pour l'entrée standard)"
    )
    detect.add_argument("--user", default=None, help="utilisateur de l'hôte (/run/media/<user>)")
    detect.add_argument(
        "--data-dir",
        default=None,
        help="dossier de données TreeAudit (signalé, et écarté des analyses, s'il est sur "
        "un disque)",
    )
    detect.add_argument(
        "--internal",
        action="store_true",
        help="proposer aussi les partitions de données du disque système (D-037)",
    )

    scan = commands.add_parser("scan", help="inventaire d'une racine")
    scan_commands = scan.add_subparsers(dest="scan_command", required=True)
    scan_run = scan_commands.add_parser("run", help="lancer un scan (Ctrl+C pour l'annuler)")
    scan_run.add_argument("root_id", help="identifiant de la racine configurée")
    scan_run.add_argument(
        "--path", default="", help="sous-dossier à analyser, relatif à la racine (défaut : tout)"
    )
    scan_run.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    scan_commands.add_parser("list", help="lister les scans enregistrés")
    scan_show = scan_commands.add_parser("show", help="afficher le résultat d'un scan")
    scan_show.add_argument("scan_id", type=int)
    parasites = scan_commands.add_parser(
        "parasites",
        help="repérer les dossiers et fichiers recréables ou inutiles (rien n'est supprimé)",
    )
    parasites.add_argument("scan_id", type=int)
    parasites.add_argument(
        "--min-confidence", type=int, default=DEFAULT_MIN_CONFIDENCE, help="seuil en pourcentage"
    )
    parasites.add_argument("--details", action="store_true", help="afficher les explications")
    parasites.add_argument(
        "--select",
        default=None,
        help="détections à regrouper dans une commande : numéros (1,3,5-7) et/ou 'green'",
    )
    parasites.add_argument(
        "--action",
        choices=[a.value for a in Action],
        default=Action.TRASH.value,
        help="commande à générer pour --select (défaut : trash, réversible)",
    )

    dupes = commands.add_parser("dupes", help="fichiers en double (rien n'est supprimé)")
    dupes_commands = dupes.add_subparsers(dest="dupes_command", required=True)
    dupes_run = dupes_commands.add_parser("run", help="chercher les doublons d'un scan")
    dupes_run.add_argument("scan_id", type=int)
    dupes_run.add_argument(
        "--min-size", type=int, default=1024, help="taille minimale en octets (défaut : 1024)"
    )
    dupes_run.add_argument(
        "--micro",
        action="store_true",
        help="micro-nettoyage : fichiers de moins de 1 Kio et fichiers vides",
    )
    dupes_run.add_argument(
        "--verify-bytes", action="store_true", help="confirmer chaque groupe octet par octet"
    )
    dupes_commands.add_parser("list", help="lister les analyses de doublons")
    dupes_show = dupes_commands.add_parser("show", help="afficher les groupes d'une analyse")
    dupes_show.add_argument("run_id", type=int)
    dupes_show.add_argument("--limit", type=int, default=30, help="groupes affichés")
    dupes_show.add_argument("--copies", type=int, default=10, help="copies affichées par groupe")
    dupes_show.add_argument(
        "--select", default=None, help="groupes pour la commande : G1,G3-5 ou 'all'"
    )
    dupes_show.add_argument(
        "--keep", type=int, default=None, help="numéro de la copie à garder (un seul groupe)"
    )
    dupes_show.add_argument(
        "--action", choices=["trash", "delete"], default="trash", help="défaut : trash"
    )

    finder = commands.add_parser(
        "search", help="chercher dans un scan (extension, glob, regex, taille, type)"
    )
    finder.add_argument("scan_id", type=int)
    finder.add_argument("--ext", default=None, help="extensions : jpg,png,.mp4")
    finder.add_argument("--name", default=None, help="nom exact")
    finder.add_argument("--glob", default=None, help="glob sur le nom : '*.py', 'IMG_????.jpg'")
    finder.add_argument(
        "--path-glob", default=None, help="glob sur le chemin relatif : 'projets/**/*.py'"
    )
    finder.add_argument("--regex", default=None, help="regex Python sur le nom")
    finder.add_argument("--path-regex", default=None, help="regex Python sur le chemin relatif")
    finder.add_argument(
        "--full-match", action="store_true", help="la regex doit couvrir tout le nom ou chemin"
    )
    finder.add_argument("-i", "--ignore-case", action="store_true", help="ignorer la casse")
    finder.add_argument(
        "--kind", default=None, help="types : file,dir,symlink,other (défaut : tous)"
    )
    finder.add_argument("--min-size", default=None, help="taille minimale : 500, 10k, 2.5M, 1G")
    finder.add_argument("--max-size", default=None, help="taille maximale")
    finder.add_argument("--limit", type=int, default=20, help="exemples affichés")
    finder.add_argument("--sort", choices=["size", "path", "mtime"], default="size")
    finder.add_argument(
        "--time-budget", type=float, default=30.0, help="durée maximale en secondes"
    )

    bench = commands.add_parser(
        "bench", help="mesurer les performances (chiffres seulement, aucun nom de fichier)"
    )
    bench.add_argument("root_id")
    bench.add_argument("--path", default="", help="sous-dossier, relatif à la racine")
    bench.add_argument("--dupes", action="store_true", help="mesurer aussi les doublons")
    bench.add_argument("--micro", action="store_true", help="et le micro-nettoyage")

    commands.add_parser("serve", help="démarrer le serveur web local")

    gateway = commands.add_parser(
        "gateway", help="relais TCP vers le conteneur app (Docker uniquement, D-020)"
    )
    gateway.add_argument("--upstream-host", required=True, help="hôte du service app")
    gateway.add_argument("--upstream-port", type=int, default=DEFAULT_PORT)
    gateway.add_argument("--listen-port", type=int, default=DEFAULT_PORT)
    return parser


# Commands that read no configuration (tools run by ./treeaudit on host data).
_WITHOUT_CONFIG = frozenset({"roots check", "archive check", "disks detect"})


# Never written inside, whatever the configuration says. /var and /run are not
# listed: the personal folder may be /var/home/<user> (Fedora Silverblue), and
# SQLite's temporary folder is in /run/user/<uid>.
_NEVER_WRITE_INSIDE = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/proc",
    "/sbin",
    "/sys",
    "/usr",
)


def _check_write_dir(path: str, home: str | None) -> None:
    """A folder the sandbox lets TreeAudit write to must be its own, never a
    system folder nor the whole personal folder (security review, D-038):
    a configuration naming ``data_dir = "/home/me"`` would otherwise open it all."""
    real = os.path.realpath(path)
    too_wide = (
        not os.path.isabs(path)
        or any(is_same_or_parent(real, forbidden) for forbidden in FORBIDDEN_ROOTS)
        or any(is_within(real, tree) for tree in _NEVER_WRITE_INSIDE)
    )
    if home is not None and is_same_or_parent(real, os.path.realpath(home)):
        too_wide = True
    if too_wide:
        raise SandboxError(
            f"dossier d'écriture refusé : {path} (ce doit être un dossier propre à TreeAudit, "
            "jamais un dossier système ni le dossier personnel entier)"
        )


def _enter_sandbox(args: argparse.Namespace, write_dirs: Sequence[str]) -> None:
    """Restrict this process for good (D-038): read anywhere, write only in
    ``write_dirs`` and SQLite's temporary folder, execute nothing, connect
    nowhere; ``serve`` may listen on its own port."""
    if not getattr(args, "sandboxed", False):
        return
    args.sandboxed = False  # once is enough: the restriction cannot be undone
    temp = os.environ.get("SQLITE_TMPDIR")
    dirs = [*write_dirs, *([temp] if temp else [])]
    for folder in dirs:
        _check_write_dir(folder, current_home())
    ports = (args.port,) if args.command == "serve" else ()
    report = restrict(Policy(write_dirs=tuple(dirs), listen_ports=ports), require_network=True)
    logger.info("kernel sandbox on (Landlock ABI %d)", report.abi)
    if not report.scoped:
        logger.warning("Landlock ABI %d: signals to other processes are not scoped", report.abi)


def _load_with_disks(args: argparse.Namespace) -> tuple[AppConfig, list[ExternalDisk]]:
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
        _enter_sandbox(
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


def _load(args: argparse.Namespace) -> AppConfig:
    return _load_with_disks(args)[0]


def _cmd_disks_detect(args: argparse.Namespace) -> int:
    """Run on the host's data (its mountinfo on stdin, its sysfs): only the disks
    accepted here are then mounted in the container, one by one, read-only."""
    mounts = _read_mounts(args.mountinfo)
    specs = system_device_specs(mounts)
    disks = find_external_disks(
        default_media_dirs(args.user),
        mounts,
        system_disks(specs),
        data_dir=args.data_dir,
        system_parts=system_partitions(specs) if args.internal else None,
    )
    _print_disks(disks, file=sys.stderr)
    if not any(disk.accepted and not disk.internal for disk in disks):
        print(
            "⚠ Aucun disque externe : branche-le, ouvre-le dans ton gestionnaire de fichiers "
            "(il doit être monté), puis relance ./treeaudit.",
            file=sys.stderr,
        )
    print(compose_override(disks, format_device_specs(specs)), end="")
    return EXIT_OK


def _print_disks(disks: Sequence[ExternalDisk], *, file: Any = None) -> None:
    out = sys.stdout if file is None else file
    if not disks:
        print("Aucun disque externe détecté.", file=out)
    for disk in disks:
        if disk.accepted:
            what = "partition interne proposée" if disk.internal else "disque proposé"
            print(f"✔ {what} : {disk.mount_point} ({disk.fs_type}) [{disk.root_id}]", file=out)
            if disk.holds_treeaudit:
                print("  (TreeAudit est installé dessus : son dossier sera ignoré)", file=out)
        else:
            print(f"✘ non proposé : {disk.mount_point} : {disk.message}", file=out)


def _cmd_disks_list(args: argparse.Namespace) -> int:
    config, disks = _load_with_disks(args)
    if not config.external_disks and not config.container_mode:
        print("Détection des disques externes désactivée (external_disks = false).")
        return EXIT_OK
    _print_disks(disks)
    return EXIT_OK


def _cmd_roots_check(args: argparse.Namespace) -> int:
    ctx = ValidationContext(
        mounts=_read_mounts(args.mountinfo),
        home=None if args.personal else args.home if args.home is not None else current_home(),
        data_dir=None if args.personal else args.data_dir,
        check_filesystem=not args.no_fs_checks,
    )
    try:
        validate_root(args.path, ctx, allow_system_volume=args.allow_system_volume or args.personal)
    except RootRejectedError as exc:
        print(f"REFUSÉ [{exc.reason.value}] {args.path} : {REASON_MESSAGES[exc.reason]}")
        return EXIT_REJECTED
    print(f"OK {args.path} peut être audité")
    if args.data_dir is None:
        print("  (chevauchement avec le dossier de données non vérifié : --data-dir absent)")
    if args.no_fs_checks:
        print("  (existence, type et liens non vérifiés : --no-fs-checks)")
    return EXIT_OK


def _read_mounts(source: str | None) -> MountTable:
    if source is None:
        return read_mountinfo()
    if source == "-":
        return parse_mountinfo(sys.stdin.buffer.read())
    return parse_mountinfo(Path(source).read_bytes())


def _cmd_archive_check(args: argparse.Namespace) -> int:
    ctx = ArchiveDirContext(
        mounts=_read_mounts(args.mountinfo),
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


def _cmd_archive_status(args: argparse.Namespace) -> int:
    config = _load(args)
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


def _cmd_archive_run(args: argparse.Namespace) -> int:
    from treeaudit.archive.output import Compression
    from treeaudit.archive.runner import (
        ArchiveProgress,
        ArchiveRequest,
        ArchiveStatus,
        InsufficientSpaceError,
        execute_archive,
    )

    config = _load(args)
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

    outcome, failure = _run_in_thread(
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
            print(
                f"REFUSÉ [{failure.reason.value}] {failure.path} : "
                f"{REASON_MESSAGES[failure.reason]}"
            )
            return EXIT_REJECTED
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
    return {
        ArchiveStatus.COMPLETE: EXIT_OK,
        ArchiveStatus.INCOMPLETE: EXIT_INCOMPLETE,
        ArchiveStatus.CANCELLED: EXIT_CANCELLED,
    }.get(status, EXIT_ERROR)


def _cmd_roots_list(args: argparse.Namespace) -> int:
    config = _load(args)
    statuses = evaluate_roots(config, read_mountinfo(), current_home())
    if not statuses:
        print("Aucune racine configurée.")
        return EXIT_OK
    for status in statuses:
        state = "OK" if status.accepted else f"REFUSÉ [{status.reason}]"
        print(f"{status.root.id}\t{state}\t{status.root.label}\t{status.root.path}")
        if status.reason is not None:
            print(f"\t{REASON_MESSAGES[status.reason]}")
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    # Imported lazily: the web stack is not needed for the other commands.
    import uvicorn

    from treeaudit.web.app import create_app

    config, disks = _load_with_disks(args)
    prepare_data_dir(config.data_dir)
    statuses = evaluate_roots(config, read_mountinfo(), current_home())
    for status in statuses:
        if status.accepted:
            logger.info("root %s accepted", status.root.id)
        else:
            logger.warning("root %s rejected: %s", status.root.id, status.reason)
    archive_status = evaluate_archive_dir(config, read_mountinfo(), current_home())
    if archive_status.enabled:
        logger.info("archiving enabled")
    elif archive_status.reason in (ArchiveDirReason.NOT_CONFIGURED, ArchiveDirReason.NOT_MOUNTED):
        logger.info("archiving disabled: no archive directory")
    else:
        logger.warning("archive directory rejected: %s", archive_status.reason)

    servers: list[uvicorn.Server] = []

    def stop() -> None:
        servers[0].should_exit = True

    declared = dataclasses.replace(config, roots=config.declared_roots)

    def detect() -> tuple[AppConfig, tuple[RootStatus, ...], Sequence[ExternalDisk]]:
        """Natively, the machine's mounts are seen live: same detection as at startup."""
        mounts = read_mountinfo()
        found, found_disks = with_external_disks(declared, mounts, os.environ)
        found = with_personal_folder(found, os.environ, current_home())
        return found, evaluate_roots(found, mounts, current_home()), found_disks

    app = create_app(
        config,
        statuses,
        external_disks=disks,
        external_enabled=config.external_disks or DISKS_ENV_VAR in os.environ,
        # In Docker, ./treeaudit stops the containers; natively there may be no terminal.
        quit=None if config.container_mode else stop,
        # In Docker the container only sees what was mounted at its start.
        detect=None if config.container_mode else detect,
    )
    servers.append(
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.bind_host,
                port=config.port,
                server_header=False,
                proxy_headers=False,
                log_level="info",
            )
        )
    )
    servers[0].run()
    # Let a cancelled job clean up (an archive removes its partial file).
    app.state.jobs.cancel()
    app.state.jobs.wait()
    return EXIT_OK


_STATUS_LABELS = {
    "complete": "complet",
    "incomplete": "INCOMPLET",
    "cancelled": "ANNULÉ",
    "failed": "ÉCHEC",
    "running": "en cours",
    "pending": "en attente",
}


def _cmd_scan_run(args: argparse.Namespace) -> int:
    import threading

    from treeaudit.scanner.models import ScanStatus
    from treeaudit.scanner.runner import ScanOutcome, ScanProgress, ScanRequest, execute_scan

    config = _load(args)
    root = config.get_root(args.root_id)
    if root is None:
        print(f"erreur : racine inconnue : {args.root_id!r}", file=sys.stderr)
        return EXIT_ERROR
    request = ScanRequest(root=root, subpath=args.path, max_depth=args.max_depth)
    cancel = threading.Event()
    progress = ScanProgress()
    result: list[ScanOutcome] = []
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(
                execute_scan(
                    config,
                    request,
                    mounts=read_mountinfo(),
                    home=current_home(),
                    cancel=cancel,
                    progress=progress,
                )
            )
        except BaseException as exc:  # reported by the main thread below
            failure.append(exc)

    thread = threading.Thread(target=worker, name="treeaudit-scan")
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
            print(
                f"\r{progress.entries} entrées, {progress.errors} erreurs, "
                f"{progress.exclusions} exclusions",
                end="",
                file=sys.stderr,
            )
    except KeyboardInterrupt:
        print("\nannulation demandée…", file=sys.stderr)
        cancel.set()
        thread.join()
    print(file=sys.stderr)

    if failure:
        exc = failure[0]
        if isinstance(exc, RootRejectedError):
            print(f"REFUSÉ [{exc.reason.value}] {exc.path} : {REASON_MESSAGES[exc.reason]}")
            return EXIT_REJECTED
        if isinstance(exc, SubpathError):
            print(f"erreur : sous-dossier refusé : {exc}", file=sys.stderr)
            return EXIT_ERROR
        raise exc
    outcome = result[0]
    print(f"scan {outcome.scan_id} : {_STATUS_LABELS[outcome.status.value]}")
    print(
        _format_totals(
            outcome.entries,
            outcome.totals.files,
            outcome.totals.dirs,
            outcome.totals.logical_size,
            outcome.totals.allocated_size,
            outcome.errors,
            outcome.exclusions,
        )
    )
    if outcome.failure:
        print(f"cause : {outcome.failure}")
    return {
        ScanStatus.COMPLETE: EXIT_OK,
        ScanStatus.INCOMPLETE: EXIT_INCOMPLETE,
        ScanStatus.CANCELLED: EXIT_CANCELLED,
    }.get(outcome.status, EXIT_ERROR)


def _format_totals(
    entries: int, files: int, dirs: int, logical: int, allocated: int, errors: int, exclusions: int
) -> str:
    return (
        f"  {entries} entrées : {files} fichiers, {dirs} dossiers ; "
        f"taille {human_size(logical)} (allouée {human_size(allocated)}) ; "
        f"{errors} erreurs, {exclusions} exclusions"
    )


def _open_index(args: argparse.Namespace) -> sqlite3.Connection:
    from treeaudit.core.datadir import ScanLock, ScanLockedError
    from treeaudit.db import dupes, scans
    from treeaudit.db.connection import connect

    config = _load(args)
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


def _cmd_scan_list(args: argparse.Namespace) -> int:
    from treeaudit.db import scans

    conn = _open_index(args)
    try:
        rows = scans.list_scans(conn)
        if not rows:
            print("Aucun scan enregistré.")
        for row in rows:
            target = row["root_id"] + (f"/{row['subpath']}" if row["subpath"] else "")
            print(
                f"{row['id']}\t{row['started_at']}\t{_STATUS_LABELS[row['status']]}\t{target}\t"
                f"{row['entries']} entrées\t{human_size(row['allocated_size'])}\t"
                f"{row['errors']} erreurs"
            )
    finally:
        conn.close()
    return EXIT_OK


def _cmd_scan_show(args: argparse.Namespace) -> int:
    from treeaudit.db import scans

    conn = _open_index(args)
    try:
        row = scans.get_scan(conn, args.scan_id)
        if row is None:
            print(f"erreur : scan inconnu : {args.scan_id}", file=sys.stderr)
            return EXIT_ERROR
        target = row["root_path"] + (f"/{row['subpath']}" if row["subpath"] else "")
        print(f"scan {row['id']} : {_STATUS_LABELS[row['status']]}  ({target})")
        print(f"  début {row['started_at']}, fin {row['finished_at'] or '-'}")
        print(
            _format_totals(
                row["entries"],
                row["files"],
                row["dirs"],
                row["logical_size"],
                row["allocated_size"],
                row["errors"],
                row["exclusions"],
            )
        )
        if row["status"] != "complete":
            print("  ATTENTION : ce scan n'est pas complet, les totaux ne couvrent pas tout.")
        if row["failure"]:
            print(f"  cause : {row['failure']}")
        print("\nExtensions (par taille) :")
        for ext in scans.top_extensions(conn, args.scan_id):
            label = f".{ext.extension}" if ext.extension else "(sans extension)"
            print(f"  {label:<20} {ext.files:>8} fichiers  {human_size(ext.logical_size):>10}")
        print("\nPlus gros dossiers (taille allouée) :")
        for d in scans.largest_dirs(conn, args.scan_id):
            print(
                f"  {human_size(d.allocated_size):>10}  {d.files:>8} fichiers  {d.rel_path_display}"
            )
        errors = scans.list_errors(conn, args.scan_id)
        if errors:
            print("\nErreurs :")
            for e in errors:
                print(f"  [{e['operation']}] {e['rel_path_display'] or '.'} : {e['message']}")
        exclusions = scans.list_exclusions(conn, args.scan_id)
        if exclusions:
            print("\nExclusions :")
            for x in exclusions:
                print(f"  [{x['reason']}] {x['rel_path_display']}")
    finally:
        conn.close()
    return EXIT_OK


def _cmd_scan_parasites(args: argparse.Namespace) -> int:
    from treeaudit.db import scans
    from treeaudit.parasites.detect import find_parasites
    from treeaudit.parasites.report import (
        SelectionError,
        parse_selection,
        render_findings,
        render_selection,
    )
    from treeaudit.parasites.rules import load_default_rules

    if not 0 <= args.min_confidence <= 100:
        print("erreur : --min-confidence doit être entre 0 et 100", file=sys.stderr)
        return EXIT_ERROR
    config = _load(args)
    conn = _open_index(args)
    try:
        row = scans.get_scan(conn, args.scan_id)
        if row is None:
            print(f"erreur : scan inconnu : {args.scan_id}", file=sys.stderr)
            return EXIT_ERROR
        root = config.get_root(row["root_id"])
        root_display = root.shown_path if root is not None else row["root_path"]
        if config.container_mode and (root is None or root.display_path is None):
            print(
                "⚠ chemin hôte inconnu : les commandes utilisent le chemin vu par le conteneur",
                file=sys.stderr,
            )
        if row["status"] != "complete":
            print(
                f"⚠ le scan {args.scan_id} n'est pas complet ({row['status']}) : "
                "des éléments peuvent manquer."
            )
        findings = find_parasites(
            conn, args.scan_id, load_default_rules(), min_confidence=args.min_confidence
        )
        print(
            render_findings(
                findings,
                root_display=root_display,
                min_confidence=args.min_confidence,
                details=args.details,
            )
        )
        if args.select is not None:
            try:
                selected = parse_selection(args.select, findings)
            except SelectionError as exc:
                print(f"erreur : {exc}", file=sys.stderr)
                return EXIT_ERROR
            print()
            print(
                render_selection(
                    selected, Action(args.action), root_display=root_display, scan_id=args.scan_id
                )
            )
    finally:
        conn.close()
    return EXIT_OK


def _run_in_thread[R](
    job: Callable[[threading.Event], R], describe: Callable[[], str]
) -> tuple[R | None, BaseException | None]:
    """Run a long job in a thread, show progress, cancel it on Ctrl+C."""
    cancel = threading.Event()
    result: list[R] = []
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(job(cancel))
        except BaseException as exc:  # reported by the caller
            failure.append(exc)

    thread = threading.Thread(target=worker, name="treeaudit-job")
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


def _cmd_dupes_run(args: argparse.Namespace) -> int:
    from treeaudit.duplicates.runner import (
        DupOptions,
        DupProgress,
        execute_duplicates,
    )
    from treeaudit.scanner.models import ScanStatus

    if args.micro:
        options = DupOptions.for_micro(verify_bytes=args.verify_bytes)
    else:
        if args.min_size < 1:
            print("erreur : --min-size doit être au moins 1", file=sys.stderr)
            return EXIT_ERROR
        options = DupOptions(min_size=args.min_size, verify_bytes=args.verify_bytes)
    config = _load(args)
    progress = DupProgress()

    def describe() -> str:
        return (
            f"{progress.stage} : {progress.files_hashed}/{progress.candidates} fichiers, "
            f"{human_size(progress.bytes_read)} lus, {progress.errors} erreurs"
        )

    outcome, failure = _run_in_thread(
        lambda cancel: execute_duplicates(
            config,
            args.scan_id,
            options,
            mounts=read_mountinfo(),
            home=current_home(),
            cancel=cancel,
            progress=progress,
        ),
        describe,
    )
    if failure is not None:
        if isinstance(failure, RootRejectedError):
            print(
                f"REFUSÉ [{failure.reason.value}] {failure.path} : "
                f"{REASON_MESSAGES[failure.reason]}"
            )
            return EXIT_REJECTED
        raise failure
    if outcome is None:  # pragma: no cover - the worker either returns or fails
        return EXIT_ERROR
    print(f"analyse {outcome.run_id} : {_STATUS_LABELS[outcome.status.value]}")
    print(
        f"  {outcome.candidates} fichiers candidats, {outcome.files_hashed} lus, "
        f"{human_size(outcome.bytes_read)} lus, {outcome.errors} erreurs"
    )
    if outcome.failure:
        print(f"cause : {outcome.failure}")
    return {
        ScanStatus.COMPLETE: EXIT_OK,
        ScanStatus.INCOMPLETE: EXIT_INCOMPLETE,
        ScanStatus.CANCELLED: EXIT_CANCELLED,
    }.get(outcome.status, EXIT_ERROR)


def _cmd_dupes_list(args: argparse.Namespace) -> int:
    from treeaudit.db import dupes

    conn = _open_index(args)
    try:
        rows = dupes.list_runs(conn)
        if not rows:
            print("Aucune analyse de doublons.")
        for row in rows:
            options = json.loads(row["options"])
            kind = "micro" if options.get("micro") else f">= {options.get('min_size')} o"
            print(
                f"{row['id']}\tscan {row['scan_id']}\t{row['started_at']}\t"
                f"{_STATUS_LABELS[row['status']]}\t{kind}\t{row['files_hashed']} lus\t"
                f"{row['errors']} erreurs"
            )
    finally:
        conn.close()
    return EXIT_OK


def _cmd_dupes_show(args: argparse.Namespace) -> int:
    from treeaudit.db import dupes, scans
    from treeaudit.duplicates.groups import load_groups
    from treeaudit.duplicates.report import (
        SelectionError,
        override_keep,
        parse_groups,
        render_command,
        render_groups,
    )
    from treeaudit.shellcmd import Action as ShellAction

    config = _load(args)
    conn = _open_index(args)
    try:
        run = dupes.get_run(conn, args.run_id)
        if run is None:
            print(f"erreur : analyse inconnue : {args.run_id}", file=sys.stderr)
            return EXIT_ERROR
        scan = scans.get_scan(conn, run["scan_id"])
        root = config.get_root(scan["root_id"]) if scan is not None else None
        root_display = root.shown_path if root is not None else (scan["root_path"] if scan else "")
        if run["status"] != "complete":
            print(
                f"⚠ l'analyse {args.run_id} n'est pas complète ({run['status']}) : "
                "des doublons peuvent manquer."
            )
        report = load_groups(conn, args.run_id)
        print(render_groups(report, limit=max(args.limit, 0), copies=max(args.copies, 1)))
        for error in dupes.list_errors(conn, args.run_id, limit=20):
            print(f"  [{error['operation']}] {error['rel_path_display']} : {error['message']}")
        if args.select is None:
            if args.keep is not None:
                print("erreur : --keep s'utilise avec --select", file=sys.stderr)
                return EXIT_ERROR
            return EXIT_OK
        try:
            selected = parse_groups(args.select, report.groups)
            if args.keep is not None:
                if len(selected) != 1:
                    raise SelectionError("--keep s'utilise avec un seul groupe")
                selected = [override_keep(selected[0], args.keep)]
        except SelectionError as exc:
            print(f"erreur : {exc}", file=sys.stderr)
            return EXIT_ERROR
        print()
        print(render_command(selected, ShellAction(args.action), root_display=root_display))
    finally:
        conn.close()
    return EXIT_OK


def _filter_spec(args: argparse.Namespace) -> FilterSpec:
    from treeaudit.filters.spec import (
        EntryKindFilter,
        FilterError,
        parse_extensions,
        parse_size,
    )

    kinds: tuple[EntryKindFilter, ...] = ()
    if args.kind:
        try:
            kinds = tuple(EntryKindFilter(k.strip()) for k in args.kind.split(",") if k.strip())
        except ValueError as exc:
            raise FilterError(f"type inconnu dans --kind : {args.kind!r}") from exc
    return FilterSpec(
        extensions=parse_extensions(args.ext) if args.ext else (),
        name_exact=args.name,
        name_glob=args.glob,
        path_glob=args.path_glob,
        name_regex=args.regex,
        path_regex=args.path_regex,
        full_match=args.full_match,
        ignore_case=args.ignore_case,
        kinds=kinds,
        min_size=parse_size(args.min_size) if args.min_size else None,
        max_size=parse_size(args.max_size) if args.max_size else None,
    )


def _cmd_search(args: argparse.Namespace) -> int:
    from treeaudit.db import scans
    from treeaudit.filters.report import render_search
    from treeaudit.filters.search import Sort, search
    from treeaudit.filters.spec import FilterError, compile_filter

    try:
        spec = _filter_spec(args)
        compiled = compile_filter(spec)
    except FilterError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.time_budget <= 0:
        print("erreur : --time-budget doit être positif", file=sys.stderr)
        return EXIT_ERROR
    conn = _open_index(args)
    try:
        row = scans.get_scan(conn, args.scan_id)
        if row is None:
            print(f"erreur : scan inconnu : {args.scan_id}", file=sys.stderr)
            return EXIT_ERROR
        sort = Sort(args.sort)
        cancel = threading.Event()
        try:
            result = search(
                conn,
                args.scan_id,
                compiled,
                limit=args.limit,
                sort=sort,
                time_budget=args.time_budget,
                cancel=cancel,
            )
        except KeyboardInterrupt:
            print("recherche interrompue", file=sys.stderr)
            return EXIT_CANCELLED
        print(
            render_search(result, spec, scan_id=args.scan_id, scan_status=row["status"], sort=sort)
        )
        return EXIT_INCOMPLETE if result.partial_reason else EXIT_OK
    finally:
        conn.close()


def _cmd_bench(args: argparse.Namespace) -> int:
    from treeaudit.bench import disk_kind, measure
    from treeaudit.db.connection import database_path
    from treeaudit.duplicates.runner import DupOptions, execute_duplicates
    from treeaudit.scanner.runner import ScanRequest, execute_scan

    config = _load(args)
    root = config.get_root(args.root_id)
    if root is None:
        print(f"erreur : racine inconnue : {args.root_id!r}", file=sys.stderr)
        return EXIT_ERROR

    def rate(count: float, seconds: float) -> str:
        return f"{count / seconds:,.0f}".replace(",", " ") if seconds > 0 else "-"

    def io_text(storage: int | None, calls: int | None) -> str:
        if storage is None or calls is None:
            return "lectures disque non mesurables ici"
        return f"lu sur le disque {human_size(storage)} en {calls} appels de lecture"

    print("Mesure TreeAudit (aucun nom de fichier n'est affiché)")
    print(f"  disque : {disk_kind(root.path)} (selon le noyau)")
    request = ScanRequest(root=root, subpath=args.path)
    scan, m = measure(
        lambda: execute_scan(
            config,
            request,
            mounts=read_mountinfo(),
            home=current_home(),
            cancel=threading.Event(),
        )
    )
    print(
        f"  analyse : {scan.entries} éléments en {m.wall:.1f} s "
        f"({rate(scan.entries, m.wall)} éléments/s), statut {scan.status.value}"
    )
    print(
        f"            processeur {m.cpu:.1f} s, mémoire max {m.peak_rss_mib} Mio, "
        f"{io_text(m.storage_read, m.read_calls)}"
    )
    runs = [("doublons", DupOptions())] if args.dupes else []
    if args.micro:
        runs.append(("micro-nettoyage", DupOptions.for_micro()))
    for label, options in runs:
        outcome, d = measure(
            lambda options=options: execute_duplicates(  # type: ignore[misc]
                config,
                scan.scan_id,
                options,
                mounts=read_mountinfo(),
                home=current_home(),
                cancel=threading.Event(),
            )
        )
        print(
            f"  {label} : {outcome.candidates} candidats, {outcome.files_hashed} lus, "
            f"{human_size(outcome.bytes_read)} en {d.wall:.1f} s "
            f"({human_size(int(outcome.bytes_read / d.wall)) if d.wall > 0 else '-'}/s), "
            f"statut {outcome.status.value}"
        )
        print(
            f"            processeur {d.cpu:.1f} s, mémoire max {d.peak_rss_mib} Mio, "
            f"{io_text(d.storage_read, d.read_calls)}"
        )
    size = database_path(config.data_dir).stat().st_size
    print(f"  base de données : {human_size(size)}")
    print(
        "Pour une mesure « à froid » (disque non mis en cache), lance avant :\n"
        "  sync; echo 3 | sudo tee /proc/sys/vm/drop_caches"
    )
    return EXIT_OK


def _cmd_gateway(args: argparse.Namespace) -> int:
    import asyncio

    from treeaudit.gateway import GatewayConfig, serve_gateway

    container_mode = is_container_mode()
    config = GatewayConfig(
        listen_host=CONTAINER_BIND_HOST if container_mode else "127.0.0.1",
        listen_port=args.listen_port,
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
    )
    asyncio.run(serve_gateway(config, container_mode=container_mode))
    return EXIT_OK


def main(argv: Sequence[str] | None = None, *, sandboxed: bool = False) -> int:
    """``sandboxed``: put this process in the kernel sandbox before any work (D-038).
    Set by the console entry point (treeaudit.entry) in the native mode only."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.sandboxed = sandboxed
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        command = f"{args.command} {getattr(args, f'{args.command}_command', '') or ''}"
        if args.sandboxed and command.strip() in _WITHOUT_CONFIG:
            _enter_sandbox(args, write_dirs=())
        if args.sandboxed and args.command == "gateway":
            # It relays connections: only meaningful, and confined, in Docker (D-020).
            raise SandboxError("treeaudit gateway ne sert qu'avec Docker")
        if args.command == "roots" and args.roots_command == "check":
            return _cmd_roots_check(args)
        if args.command == "roots" and args.roots_command == "list":
            return _cmd_roots_list(args)
        if args.command == "archive" and args.archive_command == "check":
            return _cmd_archive_check(args)
        if args.command == "archive" and args.archive_command == "status":
            return _cmd_archive_status(args)
        if args.command == "archive" and args.archive_command == "run":
            return _cmd_archive_run(args)
        if args.command == "disks" and args.disks_command == "detect":
            return _cmd_disks_detect(args)
        if args.command == "disks" and args.disks_command == "list":
            return _cmd_disks_list(args)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "scan" and args.scan_command == "run":
            return _cmd_scan_run(args)
        if args.command == "scan" and args.scan_command == "list":
            return _cmd_scan_list(args)
        if args.command == "scan" and args.scan_command == "show":
            return _cmd_scan_show(args)
        if args.command == "scan" and args.scan_command == "parasites":
            return _cmd_scan_parasites(args)
        if args.command == "dupes" and args.dupes_command == "run":
            return _cmd_dupes_run(args)
        if args.command == "dupes" and args.dupes_command == "list":
            return _cmd_dupes_list(args)
        if args.command == "dupes" and args.dupes_command == "show":
            return _cmd_dupes_show(args)
        if args.command == "bench":
            return _cmd_bench(args)
        if args.command == "search":
            return _cmd_search(args)
        if args.command == "gateway":
            return _cmd_gateway(args)
    except BrokenPipeError:
        # Output piped into a command that stopped reading (e.g. "| head"): stop
        # quietly. Closing stdout here avoids a second error when Python flushes it.
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.close()
        return EXIT_OK
    except (TreeAuditError, MountInfoError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_ERROR
    parser.error("commande inconnue")
    return EXIT_ERROR  # pragma: no cover - parser.error exits
