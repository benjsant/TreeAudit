"""Evaluation of all configured roots at startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.errors import ConfigError
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import (
    RejectReason,
    RootRejectedError,
    ValidationContext,
    check_data_dir_overlap,
    validate_root,
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


def check_data_dir_separation(config: AppConfig) -> None:
    """Refuse a configuration where the data directory overlaps any root.

    This is a configuration error (the application must not start), not a
    rejected root: TreeAudit would otherwise write inside an audited tree.
    """
    for root in config.roots:
        try:
            check_data_dir_overlap(root.path, config.data_dir)
        except RootRejectedError as exc:
            raise ConfigError(
                f"data_dir {config.data_dir!r} overlaps root {root.id!r}: "
                "TreeAudit must never write inside an audited tree"
            ) from exc


def evaluate_roots(
    config: AppConfig, mounts: MountTable, home: str | None
) -> tuple[RootStatus, ...]:
    """Validate every configured root. Rejected roots stay listed with their reason."""
    check_data_dir_separation(config)
    ctx = ValidationContext(
        mounts=mounts,
        home=home,
        data_dir=config.data_dir,
        check_filesystem=True,
        require_read_only_mount=config.container_mode,
    )
    statuses: list[RootStatus] = []
    for root in config.roots:
        try:
            validate_root(root.path, ctx, allow_system_volume=root.allow_system_volume)
        except RootRejectedError as exc:
            statuses.append(RootStatus(root=root, reason=exc.reason))
        else:
            statuses.append(RootStatus(root=root))
    return tuple(statuses)
