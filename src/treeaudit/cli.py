"""Command-line entry point.

Exit codes: 0 success, 1 root rejected, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from treeaudit import __version__
from treeaudit.core.config import (
    CONTAINER_BIND_HOST,
    DEFAULT_PORT,
    AppConfig,
    default_config_path,
    is_container_mode,
    load_config,
)
from treeaudit.core.datadir import prepare_data_dir
from treeaudit.core.errors import TreeAuditError
from treeaudit.roots.mountinfo import MountInfoError, parse_mountinfo, read_mountinfo
from treeaudit.roots.registry import current_home, evaluate_roots
from treeaudit.roots.validation import (
    REASON_MESSAGES,
    RootRejectedError,
    ValidationContext,
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
        "--no-fs-checks",
        action="store_true",
        help="ne pas accéder au chemin (existence, type, liens) : à utiliser seulement quand "
        "ces vérifications ont déjà été faites sur la machine qui possède le chemin",
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

    commands.add_parser("serve", help="démarrer le serveur web local")

    gateway = commands.add_parser(
        "gateway", help="relais TCP vers le conteneur app (Docker uniquement, D-020)"
    )
    gateway.add_argument("--upstream-host", required=True, help="hôte du service app")
    gateway.add_argument("--upstream-port", type=int, default=DEFAULT_PORT)
    gateway.add_argument("--listen-port", type=int, default=DEFAULT_PORT)
    return parser


def _load(args: argparse.Namespace) -> AppConfig:
    path = args.config if args.config is not None else default_config_path()
    return load_config(path, container_mode=is_container_mode())


def _cmd_roots_check(args: argparse.Namespace) -> int:
    if args.mountinfo is None:
        mounts = read_mountinfo()
    elif args.mountinfo == "-":
        mounts = parse_mountinfo(sys.stdin.buffer.read())
    else:
        mounts = parse_mountinfo(Path(args.mountinfo).read_bytes())

    ctx = ValidationContext(
        mounts=mounts,
        home=args.home if args.home is not None else current_home(),
        data_dir=args.data_dir,
        check_filesystem=not args.no_fs_checks,
    )
    try:
        validate_root(args.path, ctx, allow_system_volume=args.allow_system_volume)
    except RootRejectedError as exc:
        print(f"REFUSÉ [{exc.reason.value}] {args.path} : {REASON_MESSAGES[exc.reason]}")
        return EXIT_REJECTED
    print(f"OK {args.path} peut être audité")
    if args.data_dir is None:
        print("  (chevauchement avec le dossier de données non vérifié : --data-dir absent)")
    if args.no_fs_checks:
        print("  (existence, type et liens non vérifiés : --no-fs-checks)")
    return EXIT_OK


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

    config = _load(args)
    prepare_data_dir(config.data_dir)
    statuses = evaluate_roots(config, read_mountinfo(), current_home())
    for status in statuses:
        if status.accepted:
            logger.info("root %s accepted", status.root.id)
        else:
            logger.warning("root %s rejected: %s", status.root.id, status.reason)

    uvicorn.run(
        create_app(statuses),
        host=config.bind_host,
        port=config.port,
        server_header=False,
        proxy_headers=False,
        log_level="info",
    )
    return EXIT_OK


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if value < 1024 or unit == "Tio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


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
    from treeaudit.db import scans
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "roots" and args.roots_command == "check":
            return _cmd_roots_check(args)
        if args.command == "roots" and args.roots_command == "list":
            return _cmd_roots_list(args)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "scan" and args.scan_command == "run":
            return _cmd_scan_run(args)
        if args.command == "scan" and args.scan_command == "list":
            return _cmd_scan_list(args)
        if args.command == "scan" and args.scan_command == "show":
            return _cmd_scan_show(args)
        if args.command == "gateway":
            return _cmd_gateway(args)
    except (TreeAuditError, MountInfoError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_ERROR
    parser.error("commande inconnue")
    return EXIT_ERROR  # pragma: no cover - parser.error exits
