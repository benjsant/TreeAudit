"""Measurement: ``bench``, numbers only, never a file name.

The bench of the proposed sub-folders is a development tool, outside the
package (tools/subfolder_bench, D-066)."""

from __future__ import annotations

import argparse
import sys
import threading

from treeaudit.cli.common import EXIT_ERROR, EXIT_OK, load
from treeaudit.core.sizes import human_size
from treeaudit.roots.mountinfo import read_mountinfo
from treeaudit.roots.registry import current_home


def bench(args: argparse.Namespace) -> int:
    """Measure a scan (and duplicates): numbers only, never a file name."""
    from treeaudit.bench import disk_kind, measure
    from treeaudit.db.connection import database_path
    from treeaudit.duplicates.runner import DupOptions, execute_duplicates
    from treeaudit.scanner.runner import ScanRequest, execute_scan

    config = load(args)
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
