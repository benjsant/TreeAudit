"""The AppImage packaging (D-038): pinned inputs, isolated Python, no surprises."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APPIMAGE = REPO / "packaging" / "appimage"
DOCKERFILE = (REPO / "docker" / "appimage.Dockerfile").read_text(encoding="utf-8")


def test_every_download_is_pinned() -> None:
    images = re.findall(r"^ARG \w+_IMAGE=(\S+)$", DOCKERFILE, flags=re.MULTILINE)
    assert images and all("@sha256:" in image for image in images)
    adds = re.findall(r"^ADD (.+?)(?=\n\S|\Z)", DOCKERFILE, flags=re.MULTILINE | re.DOTALL)
    assert adds and all(re.search(r"--checksum=sha256:[0-9a-f]{64}", add) for add in adds)
    assert "--require-hashes" in DOCKERFILE and "--frozen" in DOCKERFILE
    assert "curl" not in DOCKERFILE and "wget" not in DOCKERFILE


def test_the_bundled_python_is_checked_to_be_the_bundled_one() -> None:
    assert "realpath" in DOCKERFILE and "test ! -L /AppDir/usr/python" in DOCKERFILE
    assert "sys.prefix == '/AppDir/usr/python'" in DOCKERFILE


def test_apprun_runs_isolated_python_and_nothing_else() -> None:
    script = (APPIMAGE / "AppRun").read_text(encoding="utf-8")
    assert script.startswith("#!/bin/bash\n") and "set -euo pipefail" in script
    assert 'exec "$PYTHON" -I -m treeaudit "$@"' in script
    for forbidden in ("eval", "rm ", "sudo", "curl", "wget", "source ", "LD_PRELOAD"):
        assert forbidden not in script, forbidden
    # The only other programs: the browser opener, and helpers to wait for the server.
    assert "xdg-open" in script and "127.0.0.1" in script


def test_desktop_entry_passes_no_argument() -> None:
    entry = (APPIMAGE / "treeaudit.desktop").read_text(encoding="utf-8")
    assert "Exec=treeaudit\n" in entry and "Terminal=false" in entry
    assert "%" not in entry  # no file or URL handed over by the desktop
