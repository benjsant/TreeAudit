"""Preparation of the TreeAudit data directory (index, reports).

This is one of the few modules allowed to write to disk, and only inside the
data directory, which never overlaps an audited root (docs/DECISIONS.md D-009).
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from treeaudit.core.errors import ConfigError

logger = logging.getLogger(__name__)

DATA_DIR_MODE = 0o700


def prepare_data_dir(data_dir: str) -> Path:
    """Create the data directory (mode 0700) if needed and check it is usable."""
    path = Path(data_dir)
    if not path.is_absolute():
        raise ConfigError(f"data_dir must be an absolute path: {data_dir!r}")
    try:
        path.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
        info = os.lstat(path)
    except OSError as exc:
        raise ConfigError(f"cannot create or access data_dir {data_dir!r}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"data_dir must be a real directory, not a link or file: {data_dir!r}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise ConfigError(f"data_dir is not writable: {data_dir!r}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        logger.warning(
            "data_dir %s is accessible to other users (mode %o); "
            "it will contain the full paths of audited files, consider chmod 700",
            data_dir,
            stat.S_IMODE(info.st_mode),
        )
    return path
