"""Run in a child process: the real web server, sandboxed, on the given port."""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.sandbox import Policy, restrict
from treeaudit.roots.mountinfo import parse_mountinfo
from treeaudit.roots.registry import evaluate_roots
from treeaudit.web.app import create_app

base, port = Path(sys.argv[1]), int(sys.argv[2])
root, data = base / "disk", base / "data"
(root / "photos").mkdir(parents=True)
(root / "photos" / "a.jpg").write_bytes(b"a")
data.mkdir(mode=0o700)
mounts = parse_mountinfo(
    b"1 0 259:3 / / rw - ext4 /dev/nvme0n1p3 rw\n"
    + f"2 1 8:1 / {base} rw - ext4 /dev/sdz1 rw\n".encode()
)
config = AppConfig(
    data_dir=str(data), roots=(RootConfig(id="disk", path=str(root), label="disk"),), port=port
)
statuses = evaluate_roots(config, mounts, home=None)
app = create_app(config, statuses, mounts=lambda: mounts, home=lambda: None)

restrict(Policy(write_dirs=(str(data),), listen_ports=(port,)))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
