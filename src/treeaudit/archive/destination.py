"""Validation of the archive directory (docs/DECISIONS.md D-024, D-032).

The archive directory is the only place outside the data directory where
TreeAudit may ever write, and only verified archives. It is optional: without
it, archiving is disabled. It is accepted only if every check passes; as for
roots, the list of system paths is a safety net, not the main protection.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum

from treeaudit.core.config import AppConfig
from treeaudit.core.errors import TreeAuditError
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import (
    FORBIDDEN_ROOTS,
    PSEUDO_TREE_EXCEPTIONS,
    is_same_or_parent,
    is_within,
    paths_overlap,
)

# Nothing may be written inside these trees, whatever the permissions allow.
SYSTEM_TREES = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/tmp",  # noqa: S108 - listed as forbidden, never used as a temp dir
    "/usr",
    "/var",
)

# Filesystems held in memory: an archive written there is lost at shutdown.
VOLATILE_FS_TYPES = frozenset({"tmpfs", "ramfs"})


class ArchiveDirReason(StrEnum):
    NOT_CONFIGURED = "not_configured"
    NOT_ABSOLUTE = "not_absolute"
    NOT_NORMALIZED = "not_normalized"
    SYSTEM_PATH = "system_path"
    HOME_DIRECTORY = "home_directory"
    NOT_MOUNTED = "not_mounted"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    NOT_A_DIRECTORY = "not_a_directory"
    SYMLINK = "symlink"
    NOT_WRITABLE = "not_writable"
    UNKNOWN_MOUNT = "unknown_mount"
    VIRTUAL_FILESYSTEM = "virtual_filesystem"
    VOLATILE_FILESYSTEM = "volatile_filesystem"
    READ_ONLY_MOUNT = "read_only_mount"
    ROOT_OVERLAP = "root_overlap"
    DATA_DIR_OVERLAP = "data_dir_overlap"


# User-facing explanations (French, see docs/DECISIONS.md D-016).
ARCHIVE_DIR_MESSAGES: dict[ArchiveDirReason, str] = {
    ArchiveDirReason.NOT_CONFIGURED: (
        "aucun dossier d'archives n'est configuré : indiquer TREEAUDIT_ARCHIVE_PATH dans .env "
        "(ou archive_dir dans la configuration)"
    ),
    ArchiveDirReason.NOT_ABSOLUTE: "le chemin doit être absolu",
    ArchiveDirReason.NOT_NORMALIZED: (
        "le chemin doit être normalisé (pas de '..', '.', '//' ni '/' final)"
    ),
    ArchiveDirReason.SYSTEM_PATH: "TreeAudit n'écrit jamais dans un dossier système",
    ArchiveDirReason.HOME_DIRECTORY: (
        "le dossier personnel entier ne peut pas servir de dossier d'archives ; "
        "choisir un sous-dossier"
    ),
    ArchiveDirReason.NOT_MOUNTED: (
        "le dossier d'archives n'est pas monté dans le conteneur : indiquer "
        "TREEAUDIT_ARCHIVE_PATH dans .env puis relancer avec make up"
    ),
    ArchiveDirReason.NOT_FOUND: "le dossier n'existe pas (le créer d'abord)",
    ArchiveDirReason.PERMISSION_DENIED: "le dossier n'est pas accessible avec les droits actuels",
    ArchiveDirReason.NOT_A_DIRECTORY: "le chemin n'est pas un dossier",
    ArchiveDirReason.SYMLINK: (
        "le chemin est un lien symbolique ou passe par un lien symbolique ; indiquer le chemin réel"
    ),
    ArchiveDirReason.NOT_WRITABLE: "le dossier n'est pas accessible en écriture",
    ArchiveDirReason.UNKNOWN_MOUNT: "impossible de déterminer le système de fichiers de ce chemin",
    ArchiveDirReason.VIRTUAL_FILESYSTEM: "ce chemin est sur un système de fichiers virtuel",
    ArchiveDirReason.VOLATILE_FILESYSTEM: (
        "ce chemin est en mémoire (tmpfs) : l'archive serait perdue à l'arrêt de la machine"
    ),
    ArchiveDirReason.READ_ONLY_MOUNT: "ce chemin est sur un volume en lecture seule",
    ArchiveDirReason.ROOT_OVERLAP: (
        "le dossier d'archives et un dossier audité ne doivent pas se chevaucher"
    ),
    ArchiveDirReason.DATA_DIR_OVERLAP: (
        "le dossier d'archives et le dossier de données de TreeAudit ne doivent pas se chevaucher"
    ),
}


class ArchiveDirRejectedError(TreeAuditError):
    """The archive directory failed validation."""

    def __init__(self, reason: ArchiveDirReason, path: str) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{path}: {ARCHIVE_DIR_MESSAGES[reason]}")


@dataclass(frozen=True, slots=True)
class ArchiveDirContext:
    """Environment against which the archive directory is validated.

    ``mounts`` is the mount table of the machine whose path is being checked.
    ``root_paths`` and ``data_dir`` are paths of that same machine.
    """

    mounts: MountTable
    home: str | None = None
    data_dir: str | None = None
    root_paths: tuple[str, ...] = ()
    check_filesystem: bool = True
    container_mode: bool = False
    """In the container, the directory must be its own (bind) mount, writable."""


@dataclass(frozen=True, slots=True)
class ArchiveDirStatus:
    """The configured archive directory and the outcome of its validation."""

    path: str | None
    shown_path: str | None
    reason: ArchiveDirReason | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None and self.reason is None

    @property
    def message(self) -> str | None:
        return None if self.reason is None else ARCHIVE_DIR_MESSAGES[self.reason]


def validate_archive_dir(path: str, ctx: ArchiveDirContext) -> None:
    """Raise ArchiveDirRejectedError if archives may not be written to ``path``."""
    _check_path_string(path)
    if ctx.home is not None and is_same_or_parent(path, os.path.normpath(ctx.home)):
        raise ArchiveDirRejectedError(ArchiveDirReason.HOME_DIRECTORY, path)
    mount = ctx.mounts.mount_for(path)
    if ctx.container_mode and (mount is None or mount.mount_point != path):
        # Otherwise archives would land in the container's own filesystem.
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_MOUNTED, path)
    if ctx.check_filesystem:
        _check_filesystem(path)
    if mount is None:
        raise ArchiveDirRejectedError(ArchiveDirReason.UNKNOWN_MOUNT, path)
    if mount.is_virtual:
        raise ArchiveDirRejectedError(ArchiveDirReason.VIRTUAL_FILESYSTEM, path)
    if mount.fs_type in VOLATILE_FS_TYPES:
        raise ArchiveDirRejectedError(ArchiveDirReason.VOLATILE_FILESYSTEM, path)
    if mount.is_read_only:
        raise ArchiveDirRejectedError(ArchiveDirReason.READ_ONLY_MOUNT, path)
    _check_overlaps(path, ctx)


def evaluate_archive_dir(
    config: AppConfig, mounts: MountTable, home: str | None
) -> ArchiveDirStatus:
    """Validate the configured archive directory; never raises for a bad one."""
    if config.archive_dir is None:
        return ArchiveDirStatus(path=None, shown_path=None, reason=ArchiveDirReason.NOT_CONFIGURED)
    ctx = ArchiveDirContext(
        mounts=mounts,
        home=home,
        data_dir=config.data_dir,
        # Declared roots only: an archive directory on a detected external disk is
        # fine; archiving a folder that contains it is refused by the runner.
        root_paths=tuple(root.path for root in config.declared_roots),
        check_filesystem=True,
        container_mode=config.container_mode,
    )
    try:
        validate_archive_dir(config.archive_dir, ctx)
    except ArchiveDirRejectedError as exc:
        reason: ArchiveDirReason | None = exc.reason
    else:
        reason = None
    return ArchiveDirStatus(
        path=config.archive_dir, shown_path=config.shown_archive_dir, reason=reason
    )


def _check_path_string(path: str) -> None:
    if not path.startswith("/"):
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_ABSOLUTE, path)
    if path.startswith("//") or os.path.normpath(path) != path:
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_NORMALIZED, path)
    if any(is_same_or_parent(path, forbidden) for forbidden in FORBIDDEN_ROOTS):
        raise ArchiveDirRejectedError(ArchiveDirReason.SYSTEM_PATH, path)
    if any(is_within(path, tree) for tree in SYSTEM_TREES) and not any(
        is_within(path, exception) for exception in PSEUDO_TREE_EXCEPTIONS
    ):
        raise ArchiveDirRejectedError(ArchiveDirReason.SYSTEM_PATH, path)


def _check_filesystem(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_FOUND, path) from exc
    except NotADirectoryError as exc:
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_FOUND, path) from exc
    except PermissionError as exc:
        raise ArchiveDirRejectedError(ArchiveDirReason.PERMISSION_DENIED, path) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ArchiveDirRejectedError(ArchiveDirReason.SYMLINK, path)
    if not stat.S_ISDIR(info.st_mode):
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_A_DIRECTORY, path)
    if os.path.realpath(path) != path:
        raise ArchiveDirRejectedError(ArchiveDirReason.SYMLINK, path)
    if not os.access(path, os.W_OK | os.X_OK):
        raise ArchiveDirRejectedError(ArchiveDirReason.NOT_WRITABLE, path)


def _check_overlaps(path: str, ctx: ArchiveDirContext) -> None:
    normalize = os.path.realpath if ctx.check_filesystem else os.path.normpath
    target = normalize(path)
    for root_path in ctx.root_paths:
        if paths_overlap(target, normalize(root_path)) or _same_host_tree(ctx, path, root_path):
            raise ArchiveDirRejectedError(ArchiveDirReason.ROOT_OVERLAP, path)
    if ctx.data_dir is not None and (
        paths_overlap(target, normalize(ctx.data_dir)) or _same_host_tree(ctx, path, ctx.data_dir)
    ):
        raise ArchiveDirRejectedError(ArchiveDirReason.DATA_DIR_OVERLAP, path)


def _same_host_tree(ctx: ArchiveDirContext, first: str, second: str) -> bool:
    """In the container, detect two bind mounts of overlapping host folders.

    Container paths never overlap (/archives, /audit/roots/<id>, /data), but the
    host folders behind them could. Each bind mount records its device and the
    folder it exposes within that filesystem (mountinfo field 4): overlapping
    folders on the same device are the same host tree. The host-side check
    (make check-root) is the main protection; this is defense in depth.
    """
    if not ctx.container_mode:
        return False
    first_mount = ctx.mounts.mount_for(first)
    second_mount = ctx.mounts.mount_for(second)
    if first_mount is None or second_mount is None:
        return False
    if first_mount.major_minor != second_mount.major_minor:
        return False
    first_tree = _host_tree(first, first_mount.mount_point, first_mount.fs_root)
    second_tree = _host_tree(second, second_mount.mount_point, second_mount.fs_root)
    return paths_overlap(first_tree, second_tree)


def _host_tree(path: str, mount_point: str, fs_root: str) -> str:
    """Path of ``path`` relative to the root of its filesystem."""
    suffix = "" if path == mount_point else path[len(mount_point.rstrip("/")) :]
    return os.path.normpath(fs_root.rstrip("/") + suffix) if suffix else fs_root
