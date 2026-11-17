"""``roots`` and ``disks``: what may be audited (D-009, D-033, D-037)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from treeaudit.cli.common import EXIT_OK, EXIT_REJECTED, load, load_with_disks, read_mounts
from treeaudit.roots.external import (
    ExternalDisk,
    compose_override,
    default_media_dirs,
    find_external_disks,
    format_device_specs,
    system_device_specs,
    system_disks,
    system_partitions,
)
from treeaudit.roots.mountinfo import read_mountinfo
from treeaudit.roots.registry import current_home, evaluate_roots
from treeaudit.roots.validation import (
    REASON_MESSAGES,
    RootRejectedError,
    ValidationContext,
    validate_root,
)


def disks_detect(args: argparse.Namespace) -> int:
    """Run on the host's data (its mountinfo on stdin, its sysfs): only the disks
    accepted here are then mounted in the container, one by one, read-only."""
    mounts = read_mounts(args.mountinfo)
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


def disks_list(args: argparse.Namespace) -> int:
    """The external disks offered or refused, and why."""
    config, disks = load_with_disks(args)
    if not config.external_disks and not config.container_mode:
        print("Détection des disques externes désactivée (external_disks = false).")
        return EXIT_OK
    _print_disks(disks)
    return EXIT_OK


def roots_check(args: argparse.Namespace) -> int:
    """Tell whether a path may be audited (run by ./treeaudit on the host)."""
    ctx = ValidationContext(
        mounts=read_mounts(args.mountinfo),
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


def roots_list(args: argparse.Namespace) -> int:
    """The configured roots and whether each is accepted."""
    config = load(args)
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
