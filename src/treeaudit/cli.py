"""Command-line entry point.

Exit codes: 0 success, 1 root rejected, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from treeaudit import __version__
from treeaudit.core.config import AppConfig, default_config_path, is_container_mode, load_config
from treeaudit.core.errors import TreeAuditError
from treeaudit.roots.mountinfo import MountInfoError, parse_mountinfo, read_mountinfo
from treeaudit.roots.registry import current_home, evaluate_roots
from treeaudit.roots.validation import (
    REASON_MESSAGES,
    RootRejectedError,
    ValidationContext,
    validate_root,
)

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_ERROR = 2

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

    commands.add_parser("serve", help="démarrer le serveur web local")
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

    from treeaudit.core.datadir import prepare_data_dir
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
    except (TreeAuditError, MountInfoError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_ERROR
    parser.error("commande inconnue")
    return EXIT_ERROR  # pragma: no cover - parser.error exits
