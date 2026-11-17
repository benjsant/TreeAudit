"""The command-line syntax: every command, option and its help (French, D-016)."""

from __future__ import annotations

import argparse
from pathlib import Path

from treeaudit import __version__
from treeaudit.core.config import DEFAULT_PORT
from treeaudit.parasites.commands import Action
from treeaudit.parasites.detect import DEFAULT_MIN_CONFIDENCE
from treeaudit.scanner.walker import DEFAULT_MAX_DEPTH


def build_parser() -> argparse.ArgumentParser:
    """Every command and option of ``treeaudit``."""
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

    commands.add_parser(
        "subfolders", help="mesurer les sous-dossiers proposés par les règles (D-050)"
    )
    commands.add_parser("serve", help="démarrer le serveur web local")

    gateway = commands.add_parser(
        "gateway", help="relais TCP vers le conteneur app (Docker uniquement, D-020)"
    )
    gateway.add_argument("--upstream-host", required=True, help="hôte du service app")
    gateway.add_argument("--upstream-port", type=int, default=DEFAULT_PORT)
    gateway.add_argument("--listen-port", type=int, default=DEFAULT_PORT)
    return parser
