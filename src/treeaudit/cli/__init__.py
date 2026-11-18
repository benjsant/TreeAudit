"""Command-line entry point: ``treeaudit <command> ...``.

Exit codes: 0 success, 1 root rejected, 2 usage or configuration error,
3 incomplete result (scan, duplicates, search, archive), 4 cancelled.

Each group of commands lives in its own module; this one only parses the
command line, enters the sandbox when asked (native mode, D-038) and calls
the command named in COMMANDS.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Callable, Sequence

from treeaudit.cli import archive, dupes, roots, scan, serve, tools
from treeaudit.cli.common import EXIT_CANCELLED, EXIT_ERROR, EXIT_INCOMPLETE, EXIT_OK, EXIT_REJECTED
from treeaudit.cli.confinement import enter_sandbox
from treeaudit.cli.parser import build_parser
from treeaudit.core.errors import TreeAuditError
from treeaudit.core.sandbox import SandboxError
from treeaudit.roots.mountinfo import MountInfoError

__all__ = [
    "EXIT_CANCELLED",
    "EXIT_ERROR",
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "EXIT_REJECTED",
    "main",
]

Command = Callable[[argparse.Namespace], int]

# (command, sub-command or "") -> the function that runs it.
COMMANDS: dict[tuple[str, str], Command] = {
    ("roots", "check"): roots.roots_check,
    ("roots", "list"): roots.roots_list,
    ("disks", "detect"): roots.disks_detect,
    ("disks", "list"): roots.disks_list,
    ("archive", "check"): archive.archive_check,
    ("archive", "status"): archive.archive_status,
    ("archive", "run"): archive.archive_run,
    ("scan", "run"): scan.scan_run,
    ("scan", "list"): scan.scan_list,
    ("scan", "show"): scan.scan_show,
    ("scan", "parasites"): scan.scan_parasites,
    ("search", ""): scan.run_search,
    ("dupes", "run"): dupes.dupes_run,
    ("dupes", "list"): dupes.dupes_list,
    ("dupes", "show"): dupes.dupes_show,
    ("serve", ""): serve.serve,
    ("gateway", ""): serve.gateway,
    ("bench", ""): tools.bench,
}

# Commands that read no configuration (tools run by ./treeaudit on host data):
# sandboxed here, before they start, since nothing else would do it.
_WITHOUT_CONFIG = frozenset({("roots", "check"), ("archive", "check"), ("disks", "detect")})


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
    key = (args.command, getattr(args, f"{args.command}_command", None) or "")
    command = COMMANDS.get(key)
    if command is None:
        parser.error("commande inconnue")  # exits
    try:
        if args.sandboxed and key in _WITHOUT_CONFIG:
            enter_sandbox(args, write_dirs=())
        if args.sandboxed and args.command == "gateway":
            # It relays connections: only meaningful, and confined, in Docker (D-020).
            raise SandboxError("treeaudit gateway ne sert qu'avec Docker")
        return command(args)
    except BrokenPipeError:
        # Output piped into a command that stopped reading (e.g. "| head"): stop
        # quietly. Closing stdout here avoids a second error when Python flushes it.
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.close()
        return EXIT_OK
    except (TreeAuditError, MountInfoError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_ERROR
