"""Shell commands proposed to the user, never executed by TreeAudit (D-025).

Shared by every feature that proposes a cleanup (parasite folders, duplicates).
Every path is shell-quoted and preceded by "--" (or "./" for tar), so a name
starting with "-" or containing quotes, spaces, "$(...)" or other shell syntax is
passed literally. Paths containing control characters (newline, tab...) or
invalid UTF-8 get no command at all: they cannot be shown unambiguously.

Two checks stand between the index and a command (double check):
1. each target is checked before it is written (check_target): strictly
   inside its root and below the analysed folder, never "/", a system tree,
   a personal folder, a disk's mount point, nor a folder holding one;
2. the finished text is read back as the shell would split it
   (verify_command) and must be exactly the expected program followed by
   targets that pass the same check, as many as were chosen. No "sudo", no
   other program, no option can slip in. A failure gives no command at all.
"""

from __future__ import annotations

import logging
import posixpath
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


_LOG = logging.getLogger(__name__)

# Never a target, nor anything inside: the system itself.
_SYSTEM_TREES = (
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
# Folders whose first levels are personal folders or disks' mount points:
# "/media/disque" or "/run/media/moi/disque" is a whole disk, never a target.
# "/media/moi/disque" cannot be told from a folder of "/media/disque" by its
# text: the disks TreeAudit knows are refused by their path (``protected``).
_MOUNT_DEPTH = {"/home": 2, "/var/home": 3, "/media": 2, "/run/media": 4, "/mnt": 2}
# Programs that must never be in a proposed command.
PRIVILEGED = frozenset({"sudo", "su", "doas", "pkexec", "run0", "sg", "chroot"})


class UnsafeTarget(ValueError):
    """A target refused by the double check; the reason is shown to the user."""


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


def _inside(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree.rstrip("/") + "/")


def check_target(path: str, root: str, scanned: str, protected: Sequence[str] = ()) -> None:
    """Raise UnsafeTarget unless ``path`` may be named in a cleanup command.

    ``root`` is the root's path, ``scanned`` the analysed folder's path, both
    absolute; ``protected`` are folders never to name nor contain (every
    root and disk TreeAudit knows). Checked on the text alone: nothing on the
    disk is looked at.
    """
    if not path.startswith("/") or posixpath.normpath(path) != path or "//" in path:
        raise UnsafeTarget(f"chemin non normalisé : {path!r}")
    if path == "/" or path.count("/") < 2:
        raise UnsafeTarget(f"dossier du système : {path}")
    in_run = _inside(path, "/run") and not _inside(path, "/run/media")
    if in_run or any(_inside(path, tree) for tree in _SYSTEM_TREES):
        raise UnsafeTarget(f"dossier du système : {path}")
    for top, depth in _MOUNT_DEPTH.items():
        if _inside(path, top) and path.count("/") <= depth:
            raise UnsafeTarget(f"disque ou dossier personnel entier : {path}")
    if any(_inside(other, path) for other in protected):
        raise UnsafeTarget(f"un disque ou un dossier proposé par TreeAudit, entier : {path}")
    if _inside(scanned, path):
        raise UnsafeTarget(f"le dossier analysé lui-même, ou un dossier qui le contient : {path}")
    if not (_inside(path, root) and _inside(path, scanned)):
        raise UnsafeTarget(f"hors du dossier analysé : {path}")


def _program(action: Action, archive_name: str, root: str) -> list[str]:
    if action is Action.ARCHIVE:
        return [
            f"mkdir -p {ARCHIVE_DIR} && tar --create --gzip",
            f'--file "$HOME/treeaudit-archives/{archive_name}"',
            f"--directory {shlex.quote(root)} --",
        ]
    return ["gio trash --" if action is Action.TRASH else "rm -rf --"]


def verify_command(
    text: str,
    action: Action,
    archive_name: str,
    root: str,
    scanned: str,
    protected: Sequence[str] = (),
) -> int:
    """Read the command back as the shell splits it; return how many targets it names.

    Raises UnsafeTarget if it is anything else than the expected program
    followed by targets that pass check_target.
    """
    words = shlex.split(text.replace(_CONTINUATION, " "))
    expected = shlex.split(" ".join(_program(action, archive_name, root)))
    head, targets = words[: len(expected)], words[len(expected) :]
    if head != expected or not targets:
        raise UnsafeTarget("la commande n'a pas la forme attendue")
    if PRIVILEGED & {posixpath.basename(w) for w in head}:
        raise UnsafeTarget("programme privilégié dans la commande")
    for target in targets:
        if action is Action.ARCHIVE:
            if not target.startswith("./"):
                raise UnsafeTarget(f"cible d'archive inattendue : {target!r}")
            target = host_path(root, target[2:])
        check_target(target, root, scanned, protected)
    return len(targets)


@dataclass(frozen=True, slots=True)
class CleanupCommand[T: CommandTarget]:
    text: str | None
    """The command, or None if no selected target can be given one."""
    skipped: tuple[T, ...]
    """Targets left out because their path cannot be quoted safely."""
    refused: tuple[tuple[T, str], ...] = ()
    """Targets refused by the double check, with the reason."""


def cleanup_command[T: CommandTarget](
    action: Action,
    targets: Sequence[T],
    root_display: str,
    *,
    scanned: str,
    protected: Sequence[str] = (),
    archive_name: str = "treeaudit-archive.tar.gz",
) -> CleanupCommand[T]:
    """One multi-line command (continued with backslashes) for all targets.

    ``scanned`` is the analysed folder, relative to the root (the scan's
    subpath): it is never a target, nor anything above it. ``protected``:
    see check_target.
    """
    if not safe_root(root_display) or not is_displayable(scanned.encode()):
        return CleanupCommand(None, tuple(targets))
    if action is Action.ARCHIVE and not _ARCHIVE_NAME.fullmatch(archive_name):
        raise ValueError(f"unsafe archive name: {archive_name!r}")
    root = host_path(root_display, "")
    scanned_path = host_path(root_display, scanned)
    usable: list[T] = []
    refused: list[tuple[T, str]] = []
    skipped = tuple(t for t in targets if not is_displayable(t.rel_path))
    for target in targets:
        if not is_displayable(target.rel_path):
            continue
        try:  # first check: each target, before it is written
            path = host_path(root_display, target.rel_path_display)
            check_target(path, root, scanned_path, protected)
        except UnsafeTarget as exc:
            refused.append((target, str(exc)))
            continue
        usable.append(target)
    if not usable:
        return CleanupCommand(None, skipped, tuple(refused))

    if action is Action.ARCHIVE:
        items = [shlex.quote(f"./{t.rel_path_display}") for t in usable]
    else:
        items = [shlex.quote(host_path(root_display, t.rel_path_display)) for t in usable]
    text = _CONTINUATION.join([*_program(action, archive_name, root), *items])
    try:  # second check: the finished text, read back as the shell would
        named = verify_command(text, action, archive_name, root, scanned_path, protected)
    except (UnsafeTarget, ValueError) as exc:
        named, reason = -1, str(exc)
    else:
        reason = f"{named} cible(s) lue(s) pour {len(usable)} choisie(s)"
    if named != len(usable):
        _LOG.error("cleanup command refused by the double check: %s", reason)
        return CleanupCommand(None, skipped, tuple((t, reason) for t in usable))
    return CleanupCommand(text, skipped, tuple(refused))
