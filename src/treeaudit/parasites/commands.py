"""Commands specific to parasite findings (D-025).

The generic cleanup commands live in ``treeaudit.shellcmd`` and are re-exported
here for convenience.
"""

from __future__ import annotations

import shlex

from treeaudit.parasites.detect import Finding
from treeaudit.shellcmd import (
    Action,
    CleanupCommand,
    cleanup_command,
    host_path,
    safe_root,
)

__all__ = ["Action", "CleanupCommand", "cleanup_command", "host_path", "recreate_command"]


def recreate_command(finding: Finding, root_display: str) -> str | None:
    """Command rebuilding the target, run from its parent directory, or None."""
    if finding.recreate is None or not finding.displayable or not safe_root(root_display):
        return None
    parent = finding.rel_path_display.rpartition("/")[0]
    command = finding.recreate.command.format(name=shlex.quote(finding.name_display))
    return f"cd -- {shlex.quote(host_path(root_display, parent))} && {command}"
