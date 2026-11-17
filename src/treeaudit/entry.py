"""Console entry point (``treeaudit``): the native mode always runs in the
kernel sandbox (docs/DECISIONS.md D-038).

Kept apart from cli.py on purpose: SQLite reads its temporary folder when it
is first loaded, so ``SQLITE_TMPDIR`` must be set before anything imports
sqlite3. In the Docker image the container is the sandbox: nothing changes.

TreeAudit refuses to start as root (D-064). Natively, the sandbox only
limits writes, and as root every file of the machine becomes readable. In
Docker the container must run with the user's ids (rule 7): ./treeaudit
refuses root too, this is the second check.
"""

from __future__ import annotations

import os
import sys

EXIT_ERROR = 2  # same code as treeaudit.cli.EXIT_ERROR, without importing it
ROOT_REFUSED = (
    "erreur : TreeAudit ne se lance pas en root (sudo). Lance-le avec ton propre compte : "
    "il lit ce que ton compte peut lire, et rien d'autre."
)


def _running_as_root() -> bool:
    """Separate function so tests can pretend to be root."""
    return os.geteuid() == 0


def run() -> int:
    """Prepare the process, then hand over to ``treeaudit.cli.main``."""
    from treeaudit.core.config import is_container_mode

    if _running_as_root():
        # Checked before anything is created: no folder of root's is touched.
        print(ROOT_REFUSED, file=sys.stderr)
        return EXIT_ERROR
    sandboxed = not is_container_mode()
    if sandboxed:
        from treeaudit.core.datadir import prepare_temp_dir

        os.environ["SQLITE_TMPDIR"] = prepare_temp_dir()
    elif os.environ.get("SQLITE_TMPDIR", "").startswith("/data/"):
        from treeaudit.core.datadir import prepare_data_dir

        prepare_data_dir(os.environ["SQLITE_TMPDIR"])  # in the data volume (compose.yaml)
    from treeaudit.cli import main

    return main(sandboxed=sandboxed)
