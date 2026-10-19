from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from treeaudit.cli import EXIT_ERROR, EXIT_OK, EXIT_REJECTED, main


def write_mountinfo(tmp_path: Path, data_point: Path) -> Path:
    """System on 259:3, tmp_path pretending to be a separate data disk."""
    escaped = str(data_point).replace(" ", "\\040")
    content = (
        "1 0 259:3 / / rw,relatime - ext4 /dev/nvme0n1p3 rw\n"
        f"2 1 8:1 / {escaped} rw,relatime - ext4 /dev/sdz1 rw\n"
    )
    path = tmp_path / "mountinfo"
    path.write_text(content, encoding="utf-8")
    return path


def test_roots_check_accepts_valid_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "audit"
    target.mkdir()
    mountinfo = write_mountinfo(tmp_path, tmp_path)
    code = main(["roots", "check", str(target), "--mountinfo", str(mountinfo), "--home", "/nope"])
    assert code == EXIT_OK
    assert capsys.readouterr().out.startswith("OK ")


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/", "forbidden_path"),
        ("/proc/1", "pseudo_filesystem_path"),
        ("relative/path", "not_absolute"),
        ("/srv/x/../y", "not_normalized"),
    ],
)
def test_roots_check_rejects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str, reason: str
) -> None:
    mountinfo = write_mountinfo(tmp_path, tmp_path)
    code = main(["roots", "check", path, "--mountinfo", str(mountinfo)])
    assert code == EXIT_REJECTED
    assert f"[{reason}]" in capsys.readouterr().out


def test_roots_check_system_volume_needs_explicit_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mountinfo = write_mountinfo(tmp_path, tmp_path)
    args = ["roots", "check", "/srv/projects", "--mountinfo", str(mountinfo), "--no-fs-checks"]
    assert main(args) == EXIT_REJECTED
    assert "[system_volume]" in capsys.readouterr().out
    assert main([*args, "--allow-system-volume"]) == EXIT_OK


def test_roots_check_reads_host_mountinfo_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    content = (
        b"1 0 259:3 / / rw - ext4 /dev/nvme0n1p3 rw\n2 1 8:1 / /mnt/Data rw - ext4 /dev/sdb1 rw\n"
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(content)))
    code = main(
        [
            "roots",
            "check",
            "/mnt/Data/Backup",
            "--mountinfo",
            "-",
            "--no-fs-checks",
            "--data-dir",
            "/home/user/treeaudit/data",
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "--no-fs-checks" in out


def test_roots_check_data_dir_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "audit"
    target.mkdir()
    mountinfo = write_mountinfo(tmp_path, tmp_path)
    code = main(
        ["roots", "check", str(target), "--mountinfo", str(mountinfo), "--data-dir", str(target)]
    )
    assert code == EXIT_REJECTED
    assert "[data_dir_overlap]" in capsys.readouterr().out


def test_invalid_mountinfo_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "mountinfo"
    bad.write_text("garbage\n", encoding="utf-8")
    assert main(["roots", "check", "/mnt/x", "--mountinfo", str(bad)]) == EXIT_ERROR
    assert "erreur" in capsys.readouterr().err


def test_roots_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\n'
        "[[roots]]\n"
        'id = "gone"\n'
        f'path = "{tmp_path / "missing"}"\n',
        encoding="utf-8",
    )
    assert main(["--config", str(config), "roots", "list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "gone" in out
    assert "not_found" in out


def test_missing_config_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(tmp_path / "none.toml"), "roots", "list"]) == EXIT_ERROR
    assert "not found" in capsys.readouterr().err
