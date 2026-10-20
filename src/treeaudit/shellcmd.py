"""Shell commands proposed to the user, never executed by TreeAudit (D-025).

Shared by every feature that proposes a cleanup (parasite folders, duplicates).
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
from typing import Protocol

ARCHIVE_DIR = '"$HOME/treeaudit-archives"'
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
_CONTINUATION = " \\\n  "
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class Action(StrEnum):
    TRASH = "trash"  # reversible: moves to the desktop trash
    DELETE = "delete"  # permanent
    ARCHIVE = "archive"  # copy into a .tar.gz, the original is left untouched


class CommandTarget(Protocol):
    """Anything that designates one entry of a root by its relative path."""

    @property
    def rel_path(self) -> bytes: ...

    @property
    def rel_path_display(self) -> str: ...


def is_displayable(raw: bytes) -> bool:
    """True if the path can be shown and quoted unambiguously in a shell command."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not _CONTROL_CHARS.search(text)


def safe_root(root_display: str) -> bool:
    return root_display.startswith("/") and is_displayable(root_display.encode())


def host_path(root_display: str, rel_path_display: str) -> str:
    root = root_display.rstrip("/") or "/"
    if not rel_path_display:
        return root
    return f"{root}/{rel_path_display}" if root != "/" else f"/{rel_path_display}"


@dataclass(frozen=True, slots=True)
class CleanupCommand[T: CommandTarget]:
    text: str | None
    """The command, or None if no selected target can be given one."""
    skipped: tuple[T, ...]
    """Targets left out because their path cannot be quoted safely."""


def cleanup_command[T: CommandTarget](
    action: Action,
    targets: Sequence[T],
    root_display: str,
    *,
    archive_name: str = "treeaudit-archive.tar.gz",
) -> CleanupCommand[T]:
    """One multi-line command (continued with backslashes) for all targets."""
    if not safe_root(root_display):
        return CleanupCommand(None, tuple(targets))
    usable = [t for t in targets if is_displayable(t.rel_path)]
    skipped = tuple(t for t in targets if not is_displayable(t.rel_path))
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
        items = [shlex.quote(f"./{t.rel_path_display}") for t in usable]
    else:
        program = "gio trash --" if action is Action.TRASH else "rm -rf --"
        head = [program]
        items = [shlex.quote(host_path(root_display, t.rel_path_display)) for t in usable]
    return CleanupCommand(_CONTINUATION.join([*head, *items]), skipped)
