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


def _scan_config(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"hello")
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\n'
        "[[roots]]\n"
        'id = "r"\n'
        f'path = "{root}"\n'
        # The test tree lives on whatever volume holds /tmp on this machine.
        "allow_system_volume = true\n",
        encoding="utf-8",
    )
    return config


def test_scan_run_list_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _scan_config(tmp_path)
    assert main(["--config", str(config), "scan", "run", "r"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "scan 1 : complet" in out
    assert "1 fichiers" in out

    assert main(["--config", str(config), "scan", "list"]) == EXIT_OK
    assert "complet" in capsys.readouterr().out

    assert main(["--config", str(config), "scan", "show", "1"]) == EXIT_OK
    shown = capsys.readouterr().out
    assert ".txt" in shown
    assert "sub" in shown


def test_scan_run_subfolder_and_unknown_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _scan_config(tmp_path)
    assert main(["--config", str(config), "scan", "run", "r", "--path", "sub"]) == EXIT_OK
    assert main(["--config", str(config), "scan", "run", "r", "--path", "../x"]) == EXIT_ERROR
    assert main(["--config", str(config), "scan", "run", "nope"]) == EXIT_ERROR
    assert main(["--config", str(config), "scan", "show", "99"]) == EXIT_ERROR
    capsys.readouterr()


def test_scan_parasites_with_selection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    (root / "app" / "node_modules" / "dep").mkdir(parents=True)
    (root / "app" / "package.json").write_bytes(b"{}")
    (root / "app" / "node_modules" / "dep" / "i.js").write_bytes(b"")
    (root / "notes" / "venv").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\n[[roots]]\nid = "r"\npath = "{root}"\n'
        'display_path = "/home/me/My Disk"\nallow_system_volume = true\n',
        encoding="utf-8",
    )
    assert main(["--config", str(config), "scan", "run", "r"]) == EXIT_OK
    capsys.readouterr()
    args = ["--config", str(config), "scan", "parasites", "1", "--select", "green"]
    assert main(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "Dépendances Node.js" in out
    assert "notes/venv" not in out  # below the default threshold
    assert "gio trash -- \\\n  '/home/me/My Disk/app/node_modules'" in out
    assert main([*args, "--action", "delete"]) == EXIT_OK
    assert "rm -rf -- \\" in capsys.readouterr().out
    assert main([*args[:-1], "99"]) == EXIT_ERROR
    assert main(["--config", str(config), "scan", "parasites", "1", "--min-confidence", "101"]) == (
        EXIT_ERROR
    )


def test_dupes_run_list_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    content = b"duplicate content " * 200
    for where in ("photos/a.jpg", "Downloads/a (1).jpg"):
        (root / where).parent.mkdir(parents=True, exist_ok=True)
        (root / where).write_bytes(content)
    for index in range(3):
        (root / f"n{index}").mkdir()
        (root / f"n{index}" / "tiny.txt").write_bytes(b"hi")
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\n[[roots]]\nid = "r"\npath = "{root}"\n'
        'display_path = "/media/disk"\nallow_system_volume = true\n',
        encoding="utf-8",
    )
    base = ["--config", str(config)]
    assert main([*base, "scan", "run", "r"]) == EXIT_OK
    assert main([*base, "dupes", "run", "1"]) == EXIT_OK
    assert "analyse 1 : complet" in capsys.readouterr().out
    assert main([*base, "dupes", "run", "1", "--micro"]) == EXIT_OK
    assert main([*base, "dupes", "list"]) == EXIT_OK
    listing = capsys.readouterr().out
    assert "micro" in listing and ">= 1024 o" in listing

    assert main([*base, "dupes", "show", "1", "--select", "all"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "G1" in out and "★" in out
    assert "gio trash -- \\\n  '/media/disk/Downloads/a (1).jpg'" in out

    assert (
        main([*base, "dupes", "show", "1", "--select", "G1", "--keep", "1", "--action", "delete"])
        == EXIT_OK
    )
    assert "rm -rf -- \\\n  /media/disk/photos/a.jpg" in capsys.readouterr().out

    assert main([*base, "dupes", "show", "2"]) == EXIT_OK
    assert "Micro-nettoyage" in capsys.readouterr().out
    assert main([*base, "dupes", "show", "1", "--keep", "1"]) == EXIT_ERROR
    assert main([*base, "dupes", "show", "1", "--select", "G7"]) == EXIT_ERROR
    assert main([*base, "dupes", "show", "99"]) == EXIT_ERROR
    assert main([*base, "dupes", "run", "99"]) == EXIT_ERROR
    capsys.readouterr()


def test_search_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _scan_config(tmp_path)
    assert main(["--config", str(config), "scan", "run", "r"]) == EXIT_OK
    capsys.readouterr()
    assert main(["--config", str(config), "search", "1", "--ext", "TXT", "-i"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "1 résultat(s)" in out and "sub/a.txt" in out
    assert "aucune commande de suppression" in out
    assert main(["--config", str(config), "search", "1", "--regex", "(a+)+"]) == EXIT_ERROR
    assert "refusée" in capsys.readouterr().err
    assert main(["--config", str(config), "search", "1", "--kind", "folder"]) == EXIT_ERROR
    assert main(["--config", str(config), "search", "1", "--min-size", "huge"]) == EXIT_ERROR
    assert main(["--config", str(config), "search", "9"]) == EXIT_ERROR


def test_archive_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mountinfo = write_mountinfo(tmp_path, tmp_path)
    base = ["archive", "check", "--mountinfo", str(mountinfo), "--no-fs-checks", "--home", "/nope"]
    assert main([*base, "/mnt/Archives"]) == EXIT_OK
    assert capsys.readouterr().out.startswith("OK ")
    assert main([*base, "/tmp/archives"]) == EXIT_REJECTED
    assert "[system_volume]" not in (out := capsys.readouterr().out)
    assert "[system_path]" in out
    overlapping = [*base, "--root", "/mnt/Archives/old", "--data-dir", "/srv/ta", "/mnt/Archives"]
    assert main(overlapping) == EXIT_REJECTED
    assert "[root_overlap]" in capsys.readouterr().out


def test_archive_check_reads_host_mountinfo_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    content = (
        b"1 0 259:3 / / rw - ext4 /dev/nvme0n1p3 rw\n2 1 8:1 / /mnt/Usb ro - ext4 /dev/sdb1 rw\n"
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(content)))
    code = main(["archive", "check", "--mountinfo", "-", "--no-fs-checks", "/mnt/Usb/archives"])
    assert code == EXIT_REJECTED
    assert "[read_only_mount]" in capsys.readouterr().out


def test_archive_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    assert main(["--config", str(config), "archive", "status"]) == EXIT_OK
    assert "Archivage désactivé" in capsys.readouterr().out
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\narchive_dir = "/etc/nope"\n', encoding="utf-8"
    )
    assert main(["--config", str(config), "archive", "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Archivage désactivé (/etc/nope)" in out
    assert "dossier système" in out


def test_archive_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from treeaudit import cli
    from treeaudit.archive import destination
    from treeaudit.roots.mountinfo import parse_mountinfo

    # pytest's tmp_path is below /tmp (refused for archives) and on tmpfs here.
    trees = tuple(tree for tree in destination.SYSTEM_TREES if tree != "/tmp")
    monkeypatch.setattr(destination, "SYSTEM_TREES", trees)
    table = parse_mountinfo(write_mountinfo(tmp_path, tmp_path).read_bytes())
    monkeypatch.setattr(cli, "read_mountinfo", lambda: table)

    source = tmp_path / "audit"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_bytes(b"a")
    archives = tmp_path / "archives"
    archives.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\narchive_dir = "{archives}"\n'
        f'[[roots]]\nid = "main"\npath = "{source}"\n',
        encoding="utf-8",
    )
    base = ["--config", str(config), "archive", "run"]
    assert main([*base, "main", "--path", "sub"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Archive complète et vérifiée" in out
    assert "même disque" in out
    assert [p.name[:4] for p in archives.iterdir()] == ["sub-"]

    assert main([*base, "main", "--path", "nope"]) == EXIT_ERROR
    assert "Archivage échoué" in capsys.readouterr().out
    assert main([*base, "other"]) == EXIT_ERROR
    assert len(list(archives.iterdir())) == 1
