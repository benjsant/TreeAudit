"""Evaluation of all configured roots at startup."""

from __future__ import annotations

import dataclasses
import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.errors import ConfigError
from treeaudit.roots.external import (
    CONTAINER_DISKS_DIR,
    SYSFS,
    ExternalDisk,
    check_mounted_disk,
    default_media_dirs,
    disks_from_environment,
    find_external_disks,
    system_device_specs,
    system_disks,
    system_partitions,
    system_specs_from_environment,
)
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.personal import CONTAINER_HOME_DIR, HOME_PATH_ENV_VAR, personal_root
from treeaudit.roots.validation import (
    RejectReason,
    RootRejectedError,
    ValidationContext,
    check_data_dir_overlap,
    validate_configured_root,
)


@dataclass(frozen=True, slots=True)
class RootStatus:
    """A configured root and the outcome of its validation."""

    root: RootConfig
    reason: RejectReason | None = None

    @property
    def accepted(self) -> bool:
        return self.reason is None


def current_home() -> str | None:
    """Return the home directory of the current user, or None if unknown."""
    try:
        return str(Path.home())
    except (RuntimeError, KeyError):
        return None


def current_user() -> str | None:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return os.environ.get("USER")


def with_external_disks(
    config: AppConfig,
    mounts: MountTable | None,
    environ: Mapping[str, str],
    *,
    sysfs: Path = SYSFS,
    user: str | None = None,
) -> tuple[AppConfig, list[ExternalDisk]]:
    """Add the accepted external disks to the roots (D-033).

    In Docker they were detected on the host and mounted one by one at
    /audit/disks/<id>; the list comes from the environment. Natively they are
    detected now, if ``external_disks`` is enabled. Returns every candidate,
    refused ones included, so that the interface can say why a disk is missing.
    """
    if config.container_mode:
        disks = disks_from_environment(environ)
        roots = [
            RootConfig(
                id=disk.root_id,
                path=f"{CONTAINER_DISKS_DIR}/{disk.root_id}",
                label=disk.name,
                display_path=disk.mount_point,
                external=True,
                internal=disk.internal,
            )
            for disk in disks
            if disk.accepted
        ]
    elif config.external_disks and mounts is not None:
        specs = system_device_specs(mounts)
        media_dirs = default_media_dirs(user if user is not None else current_user())
        disks = find_external_disks(
            media_dirs,
            mounts,
            system_disks(specs, sysfs),
            data_dir=config.data_dir,
            sysfs=sysfs,
            system_parts=system_partitions(specs, sysfs),
        )
        roots = [
            RootConfig(
                id=disk.root_id,
                path=disk.mount_point,
                label=disk.name,
                external=True,
                internal=disk.internal,
            )
            for disk in disks
            if disk.accepted
        ]
    else:
        return config, []
    return dataclasses.replace(config, roots=config.declared_roots + tuple(roots)), disks


def with_personal_folder(
    config: AppConfig, environ: Mapping[str, str], home: str | None
) -> AppConfig:
    """Add the user's personal folder as a root (D-037).

    In Docker, ./treeaudit mounts it read-only at /audit/home and passes its
    host path; natively it is offered when ``personal_home`` is enabled.
    """
    if config.container_mode:
        shown = environ.get(HOME_PATH_ENV_VAR, "")
        if not shown.startswith("/"):
            return config
        root = personal_root(CONTAINER_HOME_DIR, display_path=shown)
    elif config.personal_home and home is not None:
        root = personal_root(home)
    else:
        return config
    return dataclasses.replace(config, roots=(*config.roots, root))


def check_data_dir_separation(config: AppConfig) -> None:
    """Refuse a configuration where the data directory overlaps a declared root.

    This is a configuration error (the application must not start), not a
    rejected root: TreeAudit would otherwise write inside an audited tree.
    Detected roots (disks, personal folder) may contain it: it is then left out
    of every walk by identity (D-037).
    """
    for root in config.declared_roots:
        try:
            check_data_dir_overlap(root.path, config.data_dir)
        except RootRejectedError as exc:
            raise ConfigError(
                f"data_dir {config.data_dir!r} overlaps root {root.id!r}: "
                "TreeAudit must never write inside an audited tree"
            ) from exc


def evaluate_roots(
    config: AppConfig,
    mounts: MountTable,
    home: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    sysfs: Path = SYSFS,
) -> tuple[RootStatus, ...]:
    """Validate every configured root. Rejected roots stay listed with their reason.

    In the container, a detected disk is also checked again: dedicated read-only
    mount, still not on a disk of the host system (D-033).
    """
    check_data_dir_separation(config)
    ctx = ValidationContext(
        mounts=mounts,
        home=home,
        data_dir=config.data_dir,
        check_filesystem=True,
        require_read_only_mount=config.container_mode,
    )
    statuses: list[RootStatus] = []
    system: frozenset[str] | None = None
    system_parts: frozenset[str] | None = None
    if config.container_mode and any(root.external for root in config.roots):
        env = os.environ if environ is None else environ
        specs = system_specs_from_environment(env)
        system = system_disks(specs, sysfs)
        system_parts = system_partitions(specs, sysfs)
    for root in config.roots:
        try:
            validate_configured_root(root, ctx)
        except RootRejectedError as exc:
            statuses.append(RootStatus(root=root, reason=exc.reason))
            continue
        if (
            root.external
            and config.container_mode
            and check_mounted_disk(
                root.path,
                mounts,
                system,
                sysfs,
                system_parts=system_parts,
                internal=root.internal,
            )
            is not None
        ):
            statuses.append(RootStatus(root=root, reason=RejectReason.NOT_EXTERNAL_DISK))
            continue
        statuses.append(RootStatus(root=root))
    return tuple(statuses)
