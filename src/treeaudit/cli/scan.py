"""``scan`` and ``search``: the inventory of a root, what it holds, what to clean."""

from __future__ import annotations

import argparse
import sys
import threading

from treeaudit.cli.common import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_INCOMPLETE,
    EXIT_OK,
    STATUS_LABELS,
    exit_code,
    load,
    open_index,
    refused_root,
    run_in_thread,
)
from treeaudit.core.sizes import human_size
from treeaudit.filters.spec import FilterSpec
from treeaudit.parasites.commands import Action
from treeaudit.roots.mountinfo import read_mountinfo
from treeaudit.roots.registry import current_home
from treeaudit.roots.validation import RootRejectedError
from treeaudit.scanner.walker import SubpathError


def scan_run(args: argparse.Namespace) -> int:
    """Walk a root (or one of its sub-folders) and record what it holds."""
    from treeaudit.scanner.runner import ScanProgress, ScanRequest, execute_scan

    config = load(args)
    root = config.get_root(args.root_id)
    if root is None:
        print(f"erreur : racine inconnue : {args.root_id!r}", file=sys.stderr)
        return EXIT_ERROR
    request = ScanRequest(root=root, subpath=args.path, max_depth=args.max_depth)
    progress = ScanProgress()

    def describe() -> str:
        return (
            f"{progress.entries} entrées, {progress.errors} erreurs, "
            f"{progress.exclusions} exclusions"
        )

    outcome, failure = run_in_thread(
        lambda cancel: execute_scan(
            config,
            request,
            mounts=read_mountinfo(),
            home=current_home(),
            cancel=cancel,
            progress=progress,
        ),
        describe,
        name="treeaudit-scan",
    )
    if failure is not None:
        if isinstance(failure, RootRejectedError):
            return refused_root(failure)
        if isinstance(failure, SubpathError):
            print(f"erreur : sous-dossier refusé : {failure}", file=sys.stderr)
            return EXIT_ERROR
        raise failure
    if outcome is None:  # pragma: no cover - the worker either returns or fails
        return EXIT_ERROR
    print(f"scan {outcome.scan_id} : {STATUS_LABELS[outcome.status.value]}")
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
    return exit_code(outcome.status.value)


def _format_totals(
    entries: int, files: int, dirs: int, logical: int, allocated: int, errors: int, exclusions: int
) -> str:
    return (
        f"  {entries} entrées : {files} fichiers, {dirs} dossiers ; "
        f"taille {human_size(logical)} (allouée {human_size(allocated)}) ; "
        f"{errors} erreurs, {exclusions} exclusions"
    )


def scan_list(args: argparse.Namespace) -> int:
    """List the recorded scans, newest first."""
    from treeaudit.db import scans

    conn = open_index(args)
    try:
        rows = scans.list_scans(conn)
        if not rows:
            print("Aucun scan enregistré.")
        for row in rows:
            target = row["root_id"] + (f"/{row['subpath']}" if row["subpath"] else "")
            print(
                f"{row['id']}\t{row['started_at']}\t{STATUS_LABELS[row['status']]}\t{target}\t"
                f"{row['entries']} entrées\t{human_size(row['allocated_size'])}\t"
                f"{row['errors']} erreurs"
            )
    finally:
        conn.close()
    return EXIT_OK


def scan_show(args: argparse.Namespace) -> int:
    """Show a scan: totals, extensions, biggest folders, errors, exclusions."""
    from treeaudit.db import scans

    conn = open_index(args)
    try:
        row = scans.get_scan(conn, args.scan_id)
        if row is None:
            print(f"erreur : scan inconnu : {args.scan_id}", file=sys.stderr)
            return EXIT_ERROR
        target = row["root_path"] + (f"/{row['subpath']}" if row["subpath"] else "")
        print(f"scan {row['id']} : {STATUS_LABELS[row['status']]}  ({target})")
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


def scan_parasites(args: argparse.Namespace) -> int:
    """Show the folders to clean, and with ``--select`` the command to run (D-025)."""
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
    config = load(args)
    conn = open_index(args)
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
                    selected,
                    Action(args.action),
                    root_display=root_display,
                    scan_id=args.scan_id,
                    scanned=row["subpath"],
                    protected=config.protected_paths(),
                )
            )
    finally:
        conn.close()
    return EXIT_OK


def _filter_spec(args: argparse.Namespace) -> FilterSpec:
    """The search criteria of ``treeaudit search``, checked like the web
    form's: FilterError for an unknown kind, extension list or size."""
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


def run_search(args: argparse.Namespace) -> int:
    """Search a scan by name, path, extension, size or kind (FILTERS_AND_REGEX.md)."""
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
    conn = open_index(args)
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
