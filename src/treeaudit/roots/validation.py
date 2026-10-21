"""Validation of audit roots (docs/DECISIONS.md D-009).

A root is accepted only if every check passes. Checks are ordered from the
cheapest and least environment-dependent (path string rules) to the ones that
touch the filesystem and the mount table. The forbidden-path list is a safety net,
not the main protection: the root still has to be explicitly declared.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum

from treeaudit.core.errors import TreeAuditError
from treeaudit.roots.mountinfo import MountTable

# Roots that must never be audited, nor any of their parents.
FORBIDDEN_ROOTS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/run/media",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",  # noqa: S108 - listed as forbidden, never used as a temp dir
        "/usr",
        "/var",
    }
)

# Trees exposing kernel or process state: nothing inside them may be audited.
PSEUDO_TREES = ("/proc", "/sys", "/dev", "/run")

# Removable media are mounted below /run/media/<user>/ by udisks (Fedora, GNOME).
PSEUDO_TREE_EXCEPTIONS = ("/run/media",)


class RejectReason(StrEnum):
    NOT_ABSOLUTE = "not_absolute"
    NOT_NORMALIZED = "not_normalized"
    FORBIDDEN_PATH = "forbidden_path"
    PSEUDO_FILESYSTEM_PATH = "pseudo_filesystem_path"
    HOME_DIRECTORY = "home_directory"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    NOT_A_DIRECTORY = "not_a_directory"
    SYMLINK = "symlink"
    UNKNOWN_MOUNT = "unknown_mount"
    VIRTUAL_FILESYSTEM = "virtual_filesystem"
    SYSTEM_VOLUME = "system_volume"
    NOT_READ_ONLY = "not_read_only"
    DATA_DIR_OVERLAP = "data_dir_overlap"
    NOT_MOUNTED = "not_mounted"
    NOT_EXTERNAL_DISK = "not_external_disk"


# User-facing explanations (French, see docs/DECISIONS.md D-016).
REASON_MESSAGES: dict[RejectReason, str] = {
    RejectReason.NOT_ABSOLUTE: "le chemin doit être absolu",
    RejectReason.NOT_NORMALIZED: (
        "le chemin doit être normalisé (pas de '..', '.', '//' ni '/' final)"
    ),
    RejectReason.FORBIDDEN_PATH: (
        "ce dossier système (ou un parent d'un dossier système) ne peut pas être audité"
    ),
    RejectReason.PSEUDO_FILESYSTEM_PATH: (
        "ce chemin appartient à un système de fichiers du noyau (/proc, /sys, /dev, /run)"
    ),
    RejectReason.HOME_DIRECTORY: (
        "le dossier personnel entier ne peut pas être audité ; choisir un sous-dossier"
    ),
    RejectReason.NOT_FOUND: "le chemin n'existe pas",
    RejectReason.PERMISSION_DENIED: "le chemin n'est pas accessible avec les droits actuels",
    RejectReason.NOT_A_DIRECTORY: "le chemin n'est pas un dossier",
    RejectReason.SYMLINK: (
        "le chemin est un lien symbolique ou passe par un lien symbolique ; indiquer le chemin réel"
    ),
    RejectReason.UNKNOWN_MOUNT: "impossible de déterminer le système de fichiers de ce chemin",
    RejectReason.VIRTUAL_FILESYSTEM: "ce chemin est sur un système de fichiers virtuel",
    RejectReason.SYSTEM_VOLUME: (
        "ce chemin est sur le volume du système en cours d'exécution ; "
        "ajouter allow_system_volume = true pour l'autoriser explicitement"
    ),
    RejectReason.NOT_READ_ONLY: "dans le conteneur, la racine doit être montée en lecture seule",
    RejectReason.DATA_DIR_OVERLAP: (
        "la racine et le dossier de données de TreeAudit ne doivent pas se chevaucher"
    ),
    RejectReason.NOT_MOUNTED: "ce dossier n'est pas monté dans le conteneur",
    RejectReason.NOT_EXTERNAL_DISK: (
        "ce disque n'a pas pu être confirmé comme disque externe (voir la liste des disques)"
    ),
}


class RootRejectedError(TreeAuditError):
    """A root failed validation."""

    def __init__(self, reason: RejectReason, path: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{path}: {REASON_MESSAGES[reason]}")


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Environment against which a root is validated.

    ``mounts`` is the mount table of the machine whose path is being checked
    (the host's, even when the check runs inside a container).
    """

    mounts: MountTable
    home: str | None = None
    data_dir: str | None = None
    check_filesystem: bool = True
    require_read_only_mount: bool = False


def validate_root(path: str, ctx: ValidationContext, *, allow_system_volume: bool = False) -> None:
    """Raise RootRejectedError if ``path`` may not be used as an audit root."""
    _check_path_string(path)
    if ctx.home is not None and is_same_or_parent(path, os.path.normpath(ctx.home)):
        raise RootRejectedError(RejectReason.HOME_DIRECTORY, path)
    if ctx.require_read_only_mount:
        # In the container a root is always its own bind mount: anything else
        # would be the container's own filesystem (root not mounted).
        target = ctx.mounts.mount_for(path)
        if target is None or target.mount_point != path:
            raise RootRejectedError(RejectReason.NOT_MOUNTED, path)
    if ctx.check_filesystem:
        _check_filesystem(path)
    _check_mount(path, ctx, allow_system_volume=allow_system_volume)
    if ctx.data_dir is not None:
        check_data_dir_overlap(path, ctx.data_dir, resolve=ctx.check_filesystem)


def check_data_dir_overlap(root_path: str, data_dir: str, *, resolve: bool = True) -> None:
    """Raise if the root contains the data directory or is contained in it."""
    normalize = os.path.realpath if resolve else os.path.normpath
    root_norm = normalize(root_path)
    data_norm = normalize(data_dir)
    if paths_overlap(root_norm, data_norm):
        raise RootRejectedError(RejectReason.DATA_DIR_OVERLAP, root_path)


def _check_path_string(path: str) -> None:
    if not path.startswith("/"):
        raise RootRejectedError(RejectReason.NOT_ABSOLUTE, path)
    # normpath keeps a leading "//" (POSIX allows it to be special), reject it too.
    if path.startswith("//") or os.path.normpath(path) != path:
        raise RootRejectedError(RejectReason.NOT_NORMALIZED, path)
    if any(is_same_or_parent(path, forbidden) for forbidden in FORBIDDEN_ROOTS):
        raise RootRejectedError(RejectReason.FORBIDDEN_PATH, path)
    if any(is_within(path, tree) for tree in PSEUDO_TREES) and not any(
        is_within(path, exception) for exception in PSEUDO_TREE_EXCEPTIONS
    ):
        raise RootRejectedError(RejectReason.PSEUDO_FILESYSTEM_PATH, path)


def _check_filesystem(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise RootRejectedError(RejectReason.NOT_FOUND, path) from exc
    except PermissionError as exc:
        raise RootRejectedError(RejectReason.PERMISSION_DENIED, path) from exc
    except NotADirectoryError as exc:
        raise RootRejectedError(RejectReason.NOT_FOUND, path) from exc
    if stat.S_ISLNK(info.st_mode):
        raise RootRejectedError(RejectReason.SYMLINK, path)
    if not stat.S_ISDIR(info.st_mode):
        raise RootRejectedError(RejectReason.NOT_A_DIRECTORY, path)
    # An ancestor may be a symlink even if the last component is not.
    if os.path.realpath(path) != path:
        raise RootRejectedError(RejectReason.SYMLINK, path)


def _check_mount(path: str, ctx: ValidationContext, *, allow_system_volume: bool) -> None:
    target = ctx.mounts.mount_for(path)
    if target is None:
        raise RootRejectedError(RejectReason.UNKNOWN_MOUNT, path)
    if target.is_virtual:
        raise RootRejectedError(RejectReason.VIRTUAL_FILESYSTEM, path)
    if ctx.require_read_only_mount and not target.is_read_only:
        raise RootRejectedError(RejectReason.NOT_READ_ONLY, path)
    if allow_system_volume:
        return
    system = ctx.mounts.mount_for("/")
    if system is None:
        raise RootRejectedError(RejectReason.UNKNOWN_MOUNT, path)
    same_device = target.major_minor == system.major_minor
    # On btrfs each subvolume may get its own device number although it lives on
    # the same filesystem: compare the backing block device too.
    same_block_device = target.source.startswith("/dev/") and target.source == system.source
    if same_device or same_block_device:
        raise RootRejectedError(RejectReason.SYSTEM_VOLUME, path)


def is_within(path: str, tree: str) -> bool:
    """True if ``path`` is ``tree`` or lies below it (normalized absolute paths)."""
    return path == tree or path.startswith(tree + "/")


def is_same_or_parent(candidate: str, other: str) -> bool:
    """True if ``candidate`` is ``other`` or one of its ancestors."""
    if candidate == "/":
        return True
    return is_within(other, candidate)


def paths_overlap(first: str, second: str) -> bool:
    """True if one path is the other or one of its ancestors."""
    return is_same_or_parent(first, second) or is_same_or_parent(second, first)
