"""Run in a child process: the real scan, duplicate search and archive, sandboxed."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path

from treeaudit.archive import destination
from treeaudit.archive.runner import ArchiveRequest, execute_archive
from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.sandbox import Policy, restrict
from treeaudit.duplicates.runner import DupOptions, execute_duplicates
from treeaudit.roots.mountinfo import parse_mountinfo
from treeaudit.scanner.runner import ScanRequest, execute_scan

base = Path(sys.argv[1])
root, data, archives = base / "disk", base / "data", base / "archives"
for folder in (root / "photos", data, archives):
    folder.mkdir(parents=True)
for name in ("a.jpg", "copy.jpg"):
    (root / "photos" / name).write_bytes(b"same picture " * 500)
mounts = parse_mountinfo(
    b"1 0 259:3 / / rw - ext4 /dev/nvme0n1p3 rw\n"
    + f"2 1 8:1 / {base} rw - ext4 /dev/sdz1 rw\n".encode()
)
config = AppConfig(
    data_dir=str(data),
    archive_dir=str(archives),
    roots=(RootConfig(id="disk", path=str(root), label="disk"),),
)

# As in tests/integration/test_archive.py: the test folder lives below /tmp.
destination.SYSTEM_TREES = tuple(t for t in destination.SYSTEM_TREES if t != "/tmp")

restrict(Policy(write_dirs=(str(data), str(archives))))

never = threading.Event()
scan = execute_scan(
    config, ScanRequest(root=config.roots[0]), mounts=mounts, home=None, cancel=never
)
dupes = execute_duplicates(
    config, scan.scan_id, DupOptions(), mounts=mounts, home=None, cancel=never
)
archive = execute_archive(
    config,
    ArchiveRequest(root=config.roots[0]),
    mounts=mounts,
    home=None,
    cancel=never,
    now=datetime(2026, 10, 22, 12, 0, 0),
)
print(
    json.dumps(
        {
            "scan": scan.status.value,
            "dupes": dupes.status.value,
            "archive": archive.status.value,
        }
    )
)
