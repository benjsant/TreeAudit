"""The AppImage packaging (D-038): pinned inputs, isolated Python, no surprises."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

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
    assert '"$PYTHON" -I -m treeaudit serve || status=$?' in script
    for forbidden in ("eval", "rm ", "sudo", "curl", "wget", "source ", "LD_PRELOAD"):
        assert forbidden not in script, forbidden
    # The only other programs: the browser opener, and helpers to wait for the server.
    assert "xdg-open" in script and "127.0.0.1" in script


def test_desktop_entry_passes_no_argument() -> None:
    entry = (APPIMAGE / "treeaudit.desktop").read_text(encoding="utf-8")
    assert "Exec=treeaudit\n" in entry and "Terminal=false" in entry
    assert "%" not in entry  # no file or URL handed over by the desktop


# --- Started from the desktop (D-062): a refusal to start must be seen ---


def fake_appdir(tmp_path: Path, python: str) -> Path:
    """A copy of AppRun whose "Python" is a system program (the test folders
    are mounted noexec, so nothing written there could run)."""
    found = shutil.which(python)
    assert found is not None
    bin_dir = tmp_path / "AppDir" / "usr" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3").symlink_to(found)
    shutil.copy(APPIMAGE / "AppRun", tmp_path / "AppDir" / "AppRun")
    return tmp_path / "AppDir" / "AppRun"


def start(apprun: Path, *args: str, kernel: str = "6.8.0-45-generic") -> tuple[int, str]:
    """Run AppRun with no terminal, as a double click does; return its exit
    status and what notify-send was given."""
    log = apprun.parent / "notified"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BASH_FUNC_notify-send%%": '() { printf "%s|" "$@" >> "' + str(log) + '"; }',
        "BASH_FUNC_uname%%": f"() {{ echo {kernel}; }}",
    }
    bash = shutil.which("bash")
    assert bash is not None
    done = subprocess.run(  # noqa: S603 - our own script, with fake programs
        [bash, str(apprun), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return done.returncode, log.read_text(encoding="utf-8") if log.exists() else ""


def test_an_old_kernel_is_explained_on_the_desktop(tmp_path: Path) -> None:
    began = time.monotonic()
    status, notified = start(fake_appdir(tmp_path, "false"), kernel="6.6.30-generic")
    assert status == 1
    assert "TreeAudit n'a pas pu démarrer" in notified
    assert "6.6.30-generic) est trop ancien" in notified and "Docker" in notified
    # The browser opener is stopped: it does not wait 20 s for a server that never comes.
    assert time.monotonic() - began < 10


@pytest.mark.parametrize("kernel", ["6.7.0", "7.3.1-202.nobara.fc44.x86_64"])
def test_another_failure_says_to_look_in_a_terminal(tmp_path: Path, kernel: str) -> None:
    status, notified = start(fake_appdir(tmp_path, "false"), kernel=kernel)
    assert status == 1
    assert "TreeAudit s'est arrêté sur une erreur|Code 1." in notified
    assert "terminal" in notified and "trop ancien" not in notified


def test_a_normal_stop_and_the_command_line_notify_nothing(tmp_path: Path) -> None:
    assert start(fake_appdir(tmp_path / "ok", "true")) == (0, "")
    # With arguments it is the command line: its errors go to the terminal.
    assert start(fake_appdir(tmp_path / "cli", "false"), "scan", "list") == (1, "")


def python_appdir(tmp_path: Path, main: str) -> Path:
    """An AppDir whose Python is the real one, as a virtual environment with a
    stand-in "treeaudit" module: only the interpreter is executed from outside
    the (noexec) test folder; the module is read, as Python always does."""
    python = tmp_path / "AppDir" / "usr" / "python"
    (python / "bin").mkdir(parents=True)
    (python / "bin" / "python3").symlink_to(sys.executable)
    (python / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    package = python / "lib" / version / "site-packages" / "treeaudit"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(main, encoding="utf-8")
    shutil.copy(APPIMAGE / "AppRun", tmp_path / "AppDir" / "AppRun")
    return tmp_path / "AppDir" / "AppRun"


def test_the_server_starts_in_isolated_mode(tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    main = (
        f"import sys\nopen({str(seen)!r}, 'w').write(f'{{sys.flags.isolated}} {{sys.argv[1:]}}')\n"
    )
    assert start(python_appdir(tmp_path, main)) == (0, "")
    assert seen.read_text(encoding="utf-8") == "1 ['serve']"


def test_a_stop_by_a_signal_is_not_an_error(tmp_path: Path) -> None:
    """Logging out or shutting down stops TreeAudit by a signal: no notification."""
    main = f"import os\nos.kill(os.getpid(), {int(signal.SIGTERM)})\n"
    assert start(python_appdir(tmp_path, main)) == (128 + signal.SIGTERM, "")
