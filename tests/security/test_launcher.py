"""Checks of the ./treeaudit launcher's settings (D-034, D-035).

The launcher decides what is mounted: these tests run its "settings" command,
which starts nothing, on a copy placed in a temporary folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    target = tmp_path / "treeaudit"
    shutil.copy(REPO / "treeaudit", target)
    return target


def settings(launcher: Path, **environ: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COMPOSE_SELINUX": "0", **environ}
    result = subprocess.run(
        ["bash", str(launcher), "settings"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_defaults_need_no_configuration(launcher: Path) -> None:
    values = settings(launcher)
    here = launcher.parent.resolve()
    assert values["TREEAUDIT_UID"] == str(os.getuid())
    assert values["TREEAUDIT_GID"] == str(os.getgid())
    assert values["TREEAUDIT_EXTERNAL_DISKS"] == "1"
    assert values["TREEAUDIT_ARCHIVE_PATH"] == f"{here}/archives"
    assert values["TREEAUDIT_ROOT_PATH"] == ""
    assert values["COMPOSE"] == (
        "docker compose -f compose.yaml -f compose.archive.yaml -f .treeaudit-disks.json"
    )


def test_env_file_is_read_as_data_and_the_environment_wins(launcher: Path) -> None:
    marker = launcher.parent / "executed"
    (launcher.parent / ".env").write_text(
        "# comment\n"
        "TREEAUDIT_ROOT_PATH=/srv/photos/   # trailing comment\n"
        "TREEAUDIT_PORT=9000\r\n"
        "TREEAUDIT_EXTERNAL_DISKS=0\n"
        f"touch {marker}\n"
        f"OTHER=$(touch {marker})\n"
        f"TREEAUDIT_ARCHIVE_PATH=$(touch {marker})\n",
        encoding="utf-8",
    )
    values = settings(launcher, TREEAUDIT_PORT="9100")
    assert values["TREEAUDIT_ROOT_PATH"] == "/srv/photos"
    assert values["TREEAUDIT_PORT"] == "9100"
    assert values["TREEAUDIT_ARCHIVE_PATH"] == f"$(touch {marker})"
    assert not marker.exists()
    assert values["COMPOSE"] == (
        "docker compose -f compose.yaml -f compose.root.yaml -f compose.archive.yaml"
    )


def test_selinux_override_is_added_when_asked(launcher: Path) -> None:
    values = settings(launcher, COMPOSE_SELINUX="1")
    assert values["COMPOSE"].startswith("docker compose -f compose.yaml -f compose.selinux.yaml ")


def test_unknown_command_fails(launcher: Path) -> None:
    result = subprocess.run(
        ["bash", str(launcher), "nope"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 2
    assert "Commande inconnue" in result.stderr
