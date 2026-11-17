"""``dupes``: exact duplicates (D-015, D-026). Nothing is ever deleted."""

from __future__ import annotations

import argparse
import json
import sys

from treeaudit.cli.common import (
    EXIT_ERROR,
    EXIT_OK,
    STATUS_LABELS,
    exit_code,
    load,
    open_index,
    refused_root,
    run_in_thread,
)
from treeaudit.core.sizes import human_size
from treeaudit.roots.mountinfo import read_mountinfo
from treeaudit.roots.registry import current_home
from treeaudit.roots.validation import RootRejectedError


def dupes_run(args: argparse.Namespace) -> int:
    """Look for exact duplicates in a scan; Ctrl+C cancels."""
    from treeaudit.duplicates.runner import (
        DupOptions,
        DupProgress,
        execute_duplicates,
    )

    if args.micro:
        options = DupOptions.for_micro(verify_bytes=args.verify_bytes)
    else:
        if args.min_size < 1:
            print("erreur : --min-size doit être au moins 1", file=sys.stderr)
            return EXIT_ERROR
        options = DupOptions(min_size=args.min_size, verify_bytes=args.verify_bytes)
    config = load(args)
    progress = DupProgress()

    def describe() -> str:
        return (
            f"{progress.stage} : {progress.files_hashed}/{progress.candidates} fichiers, "
            f"{human_size(progress.bytes_read)} lus, {progress.errors} erreurs"
        )

    outcome, failure = run_in_thread(
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
            return refused_root(failure)
        raise failure
    if outcome is None:  # pragma: no cover - the worker either returns or fails
        return EXIT_ERROR
    print(f"analyse {outcome.run_id} : {STATUS_LABELS[outcome.status.value]}")
    print(
        f"  {outcome.candidates} fichiers candidats, {outcome.files_hashed} lus, "
        f"{human_size(outcome.bytes_read)} lus, {outcome.errors} erreurs"
    )
    if outcome.failure:
        print(f"cause : {outcome.failure}")
    return exit_code(outcome.status.value)


def dupes_list(args: argparse.Namespace) -> int:
    """List the searches for duplicates, newest first."""
    from treeaudit.db import dupes

    conn = open_index(args)
    try:
        rows = dupes.list_runs(conn)
        if not rows:
            print("Aucune analyse de doublons.")
        for row in rows:
            options = json.loads(row["options"])
            kind = "micro" if options.get("micro") else f">= {options.get('min_size')} o"
            print(
                f"{row['id']}\tscan {row['scan_id']}\t{row['started_at']}\t"
                f"{STATUS_LABELS[row['status']]}\t{kind}\t{row['files_hashed']} lus\t"
                f"{row['errors']} erreurs"
            )
    finally:
        conn.close()
    return EXIT_OK


def dupes_show(args: argparse.Namespace) -> int:
    """Show the groups of a search, and with ``--select`` the command to run (D-025)."""
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

    config = load(args)
    conn = open_index(args)
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
        print(
            render_command(
                selected,
                ShellAction(args.action),
                root_display=root_display,
                scanned=scan["subpath"] if scan is not None else "",
                protected=config.protected_paths(),
            )
        )
    finally:
        conn.close()
    return EXIT_OK
