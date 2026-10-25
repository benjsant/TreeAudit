"""Projects and software (D-044): found from the index, outermost only."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.db.connection import connect
from treeaudit.parasites.detect import Band
from treeaudit.projects.detect import PROJECT, SOFTWARE, Found, find_projects
from treeaudit.roots.mountinfo import MountTable
from treeaudit.scanner.runner import ScanRequest, execute_scan

OLD = 1_400_000_000  # 2014
NEW = 1_700_000_000  # 2023


def touch(path: Path, content: bytes = b"x", mtime: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    root = tmp_path / "disque"
    # An old web project whose dependencies were installed recently.
    site = root / "Code" / "mon-site"
    touch(site / "package.json", b"{}", OLD)
    touch(site / "package-lock.json", b"{}", OLD)
    touch(site / "src" / "index.js", b"js", OLD)
    touch(site / "node_modules" / "lib" / "package.json", b"{}", NEW)
    touch(site / "node_modules" / "lib" / "index.js", b"x" * 5000, NEW)
    # A monorepo: its packages are not counted apart.
    mono = root / "Code" / "mono"
    (mono / ".git").mkdir(parents=True)
    touch(mono / "Cargo.toml", b"[workspace]", NEW)
    touch(mono / "crates" / "a" / "Cargo.toml", b"[package]", NEW)
    touch(mono / "crates" / "b" / "pyproject.toml", b"[project]", NEW)
    # A tool cache: other people's packages, not projects.
    touch(root / ".cache" / "pip" / "x" / "setup.py", b"", NEW)
    # Software.
    touch(root / ".local" / "share" / "Steam" / "steamapps" / "common" / "jeu" / "jeu.bin")
    touch(root / "Windows" / "Appli" / "appli.exe")
    touch(root / "Windows" / "Appli" / "moteur.dll")
    touch(root / "Windows" / "lisezmoi.exe")  # an exe alone is not a program
    touch(root / "Outils" / "Krita.AppImage")
    # Neither.
    touch(root / "Photos" / "IMG_1.jpg")
    return root


def scan(tmp_path: Path, root: Path, table: Callable[..., MountTable]) -> list[Found]:
    config = AppConfig(
        data_dir=str(tmp_path / "data"), roots=(RootConfig(id="r", path=str(root), label="r"),)
    )
    outcome = execute_scan(
        config,
        ScanRequest(root=config.roots[0]),
        mounts=table(str(tmp_path)),
        home=None,
        cancel=threading.Event(),
    )
    conn = connect(config.data_dir)
    try:
        return find_projects(conn, outcome.scan_id)
    finally:
        conn.close()


def by_path(found: list[Found]) -> dict[str, Found]:
    return {f.rel_path_display: f for f in found}


def test_outermost_projects_and_software(
    tmp_path: Path, disk: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    found = by_path(scan(tmp_path, disk, data_disk_table))
    assert set(found) == {
        "Code/mon-site",
        "Code/mono",
        ".local/share/Steam",
        "Windows/Appli",
        "Outils/Krita.AppImage",
    }
    assert found["Code/mon-site"].group == PROJECT
    assert found["Code/mon-site"].kinds == ("JavaScript / TypeScript",)
    assert found["Code/mono"].kinds == ("Dépôt git", "Rust")
    assert found["Code/mono"].nested == 2
    assert found[".local/share/Steam"].kinds == ("Bibliothèque Steam",)
    assert found["Windows/Appli"].kinds == ("Programme Windows",)
    image = found["Outils/Krita.AppImage"]
    assert (image.group, image.is_dir, image.files) == (SOFTWARE, False, 1)


def test_recreatable_folders_and_real_last_change(
    tmp_path: Path, disk: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    site = by_path(scan(tmp_path, disk, data_disk_table))["Code/mon-site"]
    assert [f.rel_path_display for f in site.recreatable] == ["Code/mon-site/node_modules"]
    assert site.recreatable[0].band is Band.GREEN
    assert site.recreatable_size == site.recreatable[0].allocated_size
    assert site.size is not None and site.size >= site.recreatable_size
    assert site.files == 5
    # Freshly installed dependencies do not make an old project recent.
    assert site.last_change_ns == OLD * 1_000_000_000


def test_the_root_itself_can_be_the_project(
    tmp_path: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    root = tmp_path / "projet"
    touch(root / "pyproject.toml", b"[project]")
    touch(root / "outils" / "setup.py", b"")
    found = scan(tmp_path, root, data_disk_table)
    assert [(f.rel_path, f.kinds, f.nested) for f in found] == [(b"", ("Python",), 1)]


def test_age_mention() -> None:
    from treeaudit.web.pages import _age

    now = time.time()
    day = 86_400 * 1_000_000_000
    assert _age(None) == "date inconnue"
    assert _age(int(now * 1e9)) == "ce mois-ci"
    assert _age(int(now * 1e9) - 100 * day) == "il y a 3 mois"
    assert _age(int(now * 1e9) - 400 * day) == "il y a 1 an"
    assert _age(int(now * 1e9) - 3000 * day) == "il y a 8 ans"
