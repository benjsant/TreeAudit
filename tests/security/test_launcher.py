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
    # The repository's own launcher, with a controlled environment.
    result = subprocess.run(  # noqa: S603
        ["bash", str(launcher), "settings"],  # noqa: S607
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
    result = subprocess.run(  # noqa: S603
        ["bash", str(launcher), "nope"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "Commande inconnue" in result.stderr


def test_personal_folder_and_internal_disks_are_on_by_default(
    launcher: Path, tmp_path: Path
) -> None:
    home = tmp_path / "me"
    home.mkdir()
    values = settings(launcher, HOME=str(home))
    assert values["TREEAUDIT_HOME"] == "1"
    assert values["TREEAUDIT_INTERNAL_DISKS"] == "1"
    assert values["TREEAUDIT_HOME_PATH"] == str(home.resolve())
    assert " -f compose.home.yaml " in values["COMPOSE"]


def test_personal_folder_can_be_turned_off_and_its_path_never_comes_from_outside(
    launcher: Path, tmp_path: Path
) -> None:
    home = tmp_path / "me"
    home.mkdir()
    off = settings(launcher, HOME=str(home), TREEAUDIT_HOME="0")
    assert off["TREEAUDIT_HOME_PATH"] == ""
    assert "compose.home.yaml" not in off["COMPOSE"]
    # The mounted path is always derived from $HOME, never taken from .env or the environment.
    (launcher.parent / ".env").write_text("TREEAUDIT_HOME_PATH=/etc\n", encoding="utf-8")
    forced = settings(launcher, HOME=str(home), TREEAUDIT_HOME_PATH="/")
    assert forced["TREEAUDIT_HOME_PATH"] == str(home.resolve())


def test_home_through_a_symbolic_link_is_mounted_by_its_real_path(
    launcher: Path, tmp_path: Path
) -> None:
    real = tmp_path / "var-home" / "me"
    real.mkdir(parents=True)
    (tmp_path / "home-link").symlink_to(tmp_path / "var-home")
    values = settings(launcher, HOME=str(tmp_path / "home-link" / "me"))
    assert values["TREEAUDIT_HOME_PATH"] == str(real.resolve())
