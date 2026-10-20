"""Shell commands proposed to the user, never executed by TreeAudit (D-025).

Every path is shell-quoted and preceded by "--" (or "./" for tar), so a name
starting with "-" or containing quotes, spaces, "$(...)" or other shell syntax is
passed literally. Paths containing control characters (newline, tab...) or
invalid UTF-8 get no command at all: they cannot be shown unambiguously.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from treeaudit.parasites.detect import Finding, is_displayable

ARCHIVE_DIR = '"$HOME/treeaudit-archives"'
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
_CONTINUATION = " \\\n  "


class Action(StrEnum):
    TRASH = "trash"  # reversible: moves to the desktop trash
    DELETE = "delete"  # permanent
    ARCHIVE = "archive"  # copy into a .tar.gz, the original is left untouched


def host_path(root_display: str, rel_path_display: str) -> str:
    root = root_display.rstrip("/") or "/"
    if not rel_path_display:
        return root
    return f"{root}/{rel_path_display}" if root != "/" else f"/{rel_path_display}"


def _safe_root(root_display: str) -> bool:
    return root_display.startswith("/") and is_displayable(root_display.encode())


def recreate_command(finding: Finding, root_display: str) -> str | None:
    """Command rebuilding the target, run from its parent directory, or None."""
    if finding.recreate is None or not finding.displayable or not _safe_root(root_display):
        return None
    parent = finding.rel_path_display.rpartition("/")[0]
    command = finding.recreate.command.format(name=shlex.quote(finding.name_display))
    return f"cd -- {shlex.quote(host_path(root_display, parent))} && {command}"


@dataclass(frozen=True, slots=True)
class CleanupCommand:
    text: str | None
    """The command, or None if no selected finding can be given one."""
    skipped: tuple[Finding, ...]
    """Findings left out because their path cannot be quoted safely."""


def cleanup_command(
    action: Action,
    findings: Sequence[Finding],
    root_display: str,
    *,
    archive_name: str = "treeaudit-archive.tar.gz",
) -> CleanupCommand:
    """One multi-line command (continued with backslashes) for all findings."""
    if not _safe_root(root_display):
        return CleanupCommand(None, tuple(findings))
    usable = [f for f in findings if f.displayable]
    skipped = tuple(f for f in findings if not f.displayable)
    if not usable:
        return CleanupCommand(None, skipped)

    if action is Action.ARCHIVE:
        if not _ARCHIVE_NAME.fullmatch(archive_name):
            raise ValueError(f"unsafe archive name: {archive_name!r}")
        head = [
            f"mkdir -p {ARCHIVE_DIR} && tar --create --gzip",
            f'--file "$HOME/treeaudit-archives/{archive_name}"',
            f"--directory {shlex.quote(host_path(root_display, ''))} --",
        ]
        items = [shlex.quote(f"./{f.rel_path_display}") for f in usable]
    else:
        program = "gio trash --" if action is Action.TRASH else "rm -rf --"
        head = [program]
        items = [shlex.quote(host_path(root_display, f.rel_path_display)) for f in usable]
    return CleanupCommand(_CONTINUATION.join([*head, *items]), skipped)
