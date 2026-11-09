"""Checks of the ./treeaudit launcher's settings (D-034, D-035).

The launcher decides what is mounted: these tests run its "settings" command,
which starts nothing, on a copy placed in a temporary folder.
"""

from __future__ import annotations

import os
import shutil
import socket
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


def test_the_former_expert_mode_is_gone_and_its_files_ignored(launcher: Path) -> None:
    """Removed (D-052): a choice left in models/.expert starts no AI container,
    and the old commands only say so, writing nothing."""
    models = launcher.parent / "models"
    models.mkdir()
    (models / "model.gguf").write_bytes(b"")
    (models / ".expert").write_text("model.gguf\n", encoding="utf-8")
    values = settings(launcher, TREEAUDIT_EXPERT="1")
    assert "llm" not in values["COMPOSE"] and "TREEAUDIT_EXPERT" not in values
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COMPOSE_SELINUX": "0"}
    for command in ("expert", "llm-download", "llm-bench"):
        done = subprocess.run(  # noqa: S603
            ["bash", str(launcher), command],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert "D-052" in done.stdout + done.stderr
    assert sorted(p.name for p in models.iterdir()) == [".expert", "model.gguf"]


# --- Prerequisites (D-059): what is missing, and the command for this distro ---

UBUNTU = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
MINT = 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'
NOBARA = 'NAME="Nobara Linux"\nID=nobara\nID_LIKE="rhel centos fedora"\n'
ROCKY = 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\n'

# A fake docker, handed to the launcher as an exported bash function: the test
# containers mount every writable folder noexec, so no fake program can run.
# "--version" prints FAKE_DOCKER_VERSION, the subcommands named in
# FAKE_DOCKER_FAIL fail, the checks succeed silently, and every other call (one
# that would build or start something) succeeds and is logged.
FAKE_DOCKER = """() {
  case "$1" in
    --version) echo "${FAKE_DOCKER_VERSION:-Docker version 29.0.0}"; return 0 ;;
  esac
  case " ${FAKE_DOCKER_FAIL:-} " in
    *" $1 "*) return 1 ;;
  esac
  case "$1 ${2:-}" in
    "info "|"compose version"|"buildx version") return 0 ;;
  esac
  echo "$*" >> "$FAKE_DOCKER_LOG"
}"""

TOOLS = ("dirname", "realpath", "id", "readlink", "sed", "uname", "cat")


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A PATH with only the tools the launcher needs (no real docker)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    return bin_dir


def fake(name: str, body: str) -> dict[str, str]:
    """An exported bash function, which the launcher's bash imports."""
    return {f"BASH_FUNC_{name}%%": body}


def kernel(release: str) -> dict[str, str]:
    return fake("uname", f"() {{ echo {release}; }}")


def launch(
    launcher: Path,
    host: Path,
    command: str,
    os_release: str = UBUNTU,
    functions: dict[str, str] | None = None,
    **environ: str,
) -> subprocess.CompletedProcess[str]:
    release = launcher.parent / "os-release"
    release.write_text(os_release, encoding="utf-8")
    env = {
        "PATH": str(host),
        "HOME": str(launcher.parent),
        "COMPOSE_SELINUX": "0",
        "TREEAUDIT_OS_RELEASE": str(release),
        "TREEAUDIT_DOCKER_SOCKET": str(launcher.parent / "no-socket"),
        "FAKE_DOCKER_LOG": str(launcher.parent / "docker.log"),
        **(functions or {}),
        **environ,
    }
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603
        [bash, str(launcher), command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def docker_calls(launcher: Path) -> list[str]:
    log = launcher.parent / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


DOCKER = fake("docker", FAKE_DOCKER)


@pytest.mark.parametrize("os_release", [UBUNTU, MINT])
def test_without_docker_the_ubuntu_family_gets_its_packages(
    launcher: Path, host: Path, os_release: str
) -> None:
    done = launch(launcher, host, "stop", os_release)
    assert done.returncode == 1
    assert "Docker n'est pas installé" in done.stderr
    assert "sudo apt install docker.io docker-compose-v2 docker-buildx" in done.stderr
    assert 'sudo usermod -aG docker "$USER"' in done.stderr
    assert "équivalents à root" in done.stderr


def test_without_docker_fedora_and_nobara_get_moby_engine(launcher: Path, host: Path) -> None:
    done = launch(launcher, host, "start", NOBARA)
    assert done.returncode == 1
    assert "sudo dnf install moby-engine docker-compose docker-buildx" in done.stderr
    assert "sudo systemctl enable --now docker" in done.stderr


def test_a_distribution_only_like_fedora_gets_the_docker_docs(launcher: Path, host: Path) -> None:
    """Rocky says ID_LIKE=fedora but has no moby-engine: no dnf command is guessed."""
    done = launch(launcher, host, "start", ROCKY)
    assert done.returncode == 1
    assert "https://docs.docker.com/engine/install/" in done.stderr
    assert "sudo" not in done.stderr


def test_os_release_is_read_as_data(launcher: Path, host: Path) -> None:
    marker = launcher.parent / "executed"
    hostile = f"ID=$(touch {marker})\nID_LIKE=`touch {marker}`\ntouch {marker}\n"
    done = launch(launcher, host, "start", hostile)
    assert done.returncode == 1
    assert "https://docs.docker.com/engine/install/" in done.stderr
    assert not marker.exists()


def test_a_missing_compose_plugin_is_named_with_both_package_sources(
    launcher: Path, host: Path
) -> None:
    done = launch(launcher, host, "stop", UBUNTU, DOCKER, FAKE_DOCKER_FAIL="compose")
    assert done.returncode == 1
    assert "plugin Docker Compose" in done.stderr
    assert "sudo apt install docker-compose-v2" in done.stderr
    assert "docker-compose-plugin à la place" in done.stderr
    assert "service Docker" not in done.stderr
    assert docker_calls(launcher) == []


def test_a_stopped_service_and_a_refused_socket_are_told_apart(launcher: Path, host: Path) -> None:
    stopped = launch(launcher, host, "stop", NOBARA, DOCKER, FAKE_DOCKER_FAIL="info")
    assert stopped.returncode == 1
    assert "plugin" not in stopped.stderr
    assert "service Docker n'est pas démarré" in stopped.stderr
    assert "sudo systemctl enable --now docker" in stopped.stderr
    if os.geteuid() == 0:
        pytest.skip("root can write to any socket")
    path = launcher.parent / "docker.sock"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        path.chmod(0o400)
        refused = launch(
            launcher,
            host,
            "stop",
            NOBARA,
            DOCKER,
            FAKE_DOCKER_FAIL="info",
            TREEAUDIT_DOCKER_SOCKET=str(path),
        )
    assert refused.returncode == 1
    assert "n'a pas accès à Docker" in refused.stderr
    assert 'sudo usermod -aG docker "$USER"' in refused.stderr
    assert "service Docker" not in refused.stderr


def test_podman_behind_the_docker_command_is_named(launcher: Path, host: Path) -> None:
    done = launch(
        launcher, host, "start", NOBARA, DOCKER, FAKE_DOCKER_VERSION="podman version 5.4.0"
    )
    assert done.returncode == 1
    assert "La commande docker est Podman" in done.stderr
    assert docker_calls(launcher) == []


def test_buildx_is_needed_only_to_build_the_appimage(launcher: Path, host: Path) -> None:
    # With every check passed, "stop" goes on to its own "exec docker compose
    # down", which a bash function cannot answer: only the checks are judged.
    stop = launch(launcher, host, "stop", UBUNTU, DOCKER, FAKE_DOCKER_FAIL="buildx")
    assert "✘" not in stop.stderr
    assert "ne peut pas" not in stop.stderr
    appimage = launch(launcher, host, "appimage", UBUNTU, DOCKER, FAKE_DOCKER_FAIL="buildx")
    assert appimage.returncode == 1
    assert "plugin Docker Buildx" in appimage.stderr
    assert "sudo apt install docker-buildx" in appimage.stderr
    assert "docker-buildx-plugin à la place" in appimage.stderr
    assert docker_calls(launcher) == []


def test_requirements_checks_and_installs_nothing(launcher: Path, host: Path) -> None:
    done = launch(launcher, host, "requirements", UBUNTU, DOCKER | kernel("6.6.30-generic"))
    assert done.returncode == 0, done.stdout
    assert "✔ Docker, Compose et l'accès au service" in done.stdout
    assert "✔ Buildx" in done.stdout
    assert "✘ noyau 6.6.30-generic" in done.stdout
    # Only the checks ran: no image built, no container started.
    assert docker_calls(launcher) == []


def test_requirements_fails_when_docker_is_missing(launcher: Path, host: Path) -> None:
    done = launch(launcher, host, "prerequis", UBUNTU, kernel("6.8.0-45-generic"))
    assert done.returncode == 1
    assert "sudo apt install docker.io" in done.stdout
    assert "✔ noyau 6.8.0-45-generic" in done.stdout


@pytest.mark.parametrize(
    ("release", "accepted"),
    [("6.7.0", True), ("7.0.1", True), ("5.19.0", False), ("6.6.99", False)],
)
def test_the_appimage_kernel_threshold(
    launcher: Path, host: Path, release: str, accepted: bool
) -> None:
    done = launch(launcher, host, "requirements", UBUNTU, DOCKER | kernel(release))
    assert (f"✔ noyau {release}" in done.stdout) is accepted
    assert (f"✘ noyau {release}" in done.stdout) is not accepted


def test_settings_and_help_need_no_docker(launcher: Path, host: Path) -> None:
    for command in ("settings", "help"):
        done = launch(launcher, host, command, UBUNTU)
        assert done.returncode == 0, done.stderr
        assert "Docker n'est pas installé" not in done.stderr
