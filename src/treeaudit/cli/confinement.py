"""The native mode's confinement, decided before any work (D-038, D-064).

Which folders the kernel sandbox lets TreeAudit write to, and the moment the
process enters it for good. Kept apart from the commands: this is security
code (rule 17 of CLAUDE.md), the commands only call ``enter_sandbox``.

Kept in the command-line package rather than in ``core``: it relies on the
root validation (``roots``), and ``core`` depends on nothing else
(tests/unit/test_architecture.py).
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence

from treeaudit.core.sandbox import Policy, SandboxError, restrict
from treeaudit.roots.registry import current_home
from treeaudit.roots.validation import FORBIDDEN_ROOTS, is_same_or_parent, is_within

logger = logging.getLogger("treeaudit")

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
    "/root",
    "/sbin",
    "/sys",
    "/usr",
)


def check_write_dir(path: str, home: str | None) -> None:
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


def enter_sandbox(args: argparse.Namespace, write_dirs: Sequence[str]) -> None:
    """Restrict this process for good (D-038): read anywhere, write only in
    ``write_dirs`` and SQLite's temporary folder, execute nothing, connect
    nowhere; ``serve`` may listen on its own port."""
    if not getattr(args, "sandboxed", False):
        return
    args.sandboxed = False  # once is enough: the restriction cannot be undone
    temp = os.environ.get("SQLITE_TMPDIR")
    dirs = [*write_dirs, *([temp] if temp else [])]
    for folder in dirs:
        check_write_dir(folder, current_home())
    ports = (args.port,) if args.command == "serve" else ()
    report = restrict(Policy(write_dirs=tuple(dirs), listen_ports=ports), require_network=True)
    logger.info("kernel sandbox on (Landlock ABI %d)", report.abi)
    if not report.scoped:
        logger.warning("Landlock ABI %d: signals to other processes are not scoped", report.abi)
