"""Console entry point (``treeaudit``): the native mode always runs in the
kernel sandbox (docs/DECISIONS.md D-038).

Kept apart from cli.py on purpose: SQLite reads its temporary folder when it
is first loaded, so ``SQLITE_TMPDIR`` must be set before anything imports
sqlite3. In the Docker image the container is the sandbox: nothing changes.
"""

from __future__ import annotations

import os


def run() -> int:
    from treeaudit.core.config import is_container_mode

    sandboxed = not is_container_mode()
    if sandboxed:
        from treeaudit.core.datadir import prepare_temp_dir

        os.environ["SQLITE_TMPDIR"] = prepare_temp_dir()
    elif os.environ.get("SQLITE_TMPDIR", "").startswith("/data/"):
        from treeaudit.core.datadir import prepare_data_dir

        prepare_data_dir(os.environ["SQLITE_TMPDIR"])  # in the data volume (compose.yaml)
    from treeaudit.cli import main

    return main(sandboxed=sandboxed)
