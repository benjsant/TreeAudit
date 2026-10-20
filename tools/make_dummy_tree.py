"""Generate a synthetic, lightweight tree to test TreeAudit without personal data.

    python tools/make_dummy_tree.py dummy_folder [--scale 1.0]

The tree imitates hard real-world cases (a Steam-like library with many tiny
files, very wide and very deep directories, odd names, links, special files)
plus material for later phases: "parasite" directories that can be rebuilt
(.venv, __pycache__, node_modules, __MACOSX...) and duplicated files.

It stays small on disk: files are empty or a few bytes long, and the only big
file is sparse (its logical size is large, its allocated size is ~0). At scale
1.0 it creates about 200 000 entries for a few tens of MB.

This is a development tool: it only writes inside the target directory, which
must not exist yet (or be empty), and it is deterministic (fixed seed).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

SEED = 20261020


def touch(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def steam_like_library(base: Path, rng: random.Random, scale: float) -> None:
    """Many games, each with many tiny asset files spread over nested folders."""
    games = max(1, int(40 * scale))
    for game in range(games):
        root = base / "steamapps" / "common" / f"Game {game:03d}"
        for pak in range(int(25 * scale) or 1):
            folder = root / "assets" / f"pak{pak:02d}" / rng.choice(["tex", "snd", "mesh"])
            for item in range(rng.randint(80, 160)):
                touch(folder / f"asset_{item:05d}.{rng.choice(['dds', 'ogg', 'bin', 'json'])}")
        touch(root / "Game.exe", b"MZ")
        touch(root / "steam_appid.txt", str(game).encode())
    touch(base / "steamapps" / f"appmanifest_{games}.acf", b'"AppState" {}')


def wide_directory(base: Path, scale: float) -> None:
    """A single directory with tens of thousands of entries (shader caches...)."""
    folder = base / "steamapps" / "shadercache" / "flat"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(int(30_000 * scale) or 1):
        (folder / f"{index:08x}.cache").write_bytes(b"")


def deep_directory(base: Path) -> None:
    path = base / "deep"
    for level in range(120):
        path = path / f"level{level}"
    path.mkdir(parents=True)
    touch(path / "bottom.txt", b"you found it")


def odd_names(base: Path) -> None:
    folder = base / "odd names"
    for name in [
        "with space.txt",
        "new\nline.txt",
        "tab\there.txt",
        "-starts-with-dash",
        ".hidden",
        "émoji 📁.txt",
        "café.txt",  # NFC
        "café.txt",  # NFD, a different name
        "<img src=x onerror=alert(1)>.html",
        "$(echo pwned).txt",
        "a" * 200 + ".txt",
    ]:
        touch(folder / name, b"x")
    raw = os.fsencode(folder) + b"/invalid-\xff\xfe-utf8.txt"
    fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)


def links_and_specials(base: Path) -> None:
    folder = base / "links"
    folder.mkdir(parents=True)
    (folder / "target.txt").write_bytes(b"target")
    (folder / "relative-link").symlink_to("target.txt")
    (folder / "to-etc").symlink_to("/etc")
    (folder / "broken").symlink_to("does-not-exist")
    (folder / "loop").symlink_to(".")
    os.link(folder / "target.txt", folder / "hardlink-to-target.txt")
    os.mkfifo(folder / "a-fifo")
    with (folder / "sparse-1GiB.img").open("wb") as handle:
        handle.truncate(1024**3)


def parasite_directories(base: Path) -> None:
    """Rebuildable folders, with the evidence real tools leave behind."""
    py = base / "projects" / "python-app"
    touch(py / "pyproject.toml", b'[project]\nname = "app"\n')
    touch(py / "requirements.txt", b"requests\n")
    touch(py / "app" / "__init__.py")
    touch(py / "app" / "__pycache__" / "__init__.cpython-313.pyc", b"\x00" * 16)
    touch(py / ".venv" / "pyvenv.cfg", b"home = /usr/bin\nversion = 3.13.0\n")
    for index in range(300):
        touch(py / ".venv" / "lib" / "python3.13" / "site-packages" / "pkg" / f"m{index}.py")
    touch(py / ".pytest_cache" / "README.md", b"pytest cache")
    touch(py / ".mypy_cache" / "3.13" / "app.data.json", b"{}")

    # A venv-looking folder WITHOUT pyvenv.cfg: must not get high confidence.
    touch(base / "projects" / "suspicious" / "venv" / "important-notes.txt", b"keep me")

    js = base / "projects" / "web-app"
    touch(js / "package.json", b'{"name": "web-app"}')
    touch(js / "package-lock.json", b"{}")
    for index in range(500):
        touch(js / "node_modules" / f"dep{index % 50}" / f"file{index}.js")
    touch(js / "dist" / "bundle.js", b"//")
    touch(js / "vendor" / "bootstrap" / "css" / "bootstrap.min.css", b"/*! Bootstrap v5.3 */")
    touch(js / "vendor" / "bootstrap" / "js" / "bootstrap.bundle.min.js", b"/*! Bootstrap */")

    cs = base / "projects" / "dotnet-app"
    touch(cs / "App.csproj", b"<Project />")
    touch(cs / "bin" / "Debug" / "App.dll", b"MZ")
    touch(cs / "obj" / "project.assets.json", b"{}")
    # A "bin" folder of personal scripts, with no build file around.
    touch(base / "home-scripts" / "bin" / "backup.sh", b"#!/bin/sh\n")

    archive = base / "downloads" / "photos-from-mac"
    touch(archive / "__MACOSX" / "._IMG_0001.jpg", b"\x00\x05\x16\x07")
    touch(archive / "IMG_0001.jpg", b"\xff\xd8\xff")
    touch(archive / ".DS_Store", b"\x00\x00\x00\x01Bud1")
    touch(base / "downloads" / "Thumbs.db", b"\xd0\xcf")


def duplicates(base: Path) -> None:
    content = b"identical content " * 10
    copies = ["docs/report-final.pdf", "backup/2024/report-final.pdf", "old/copy of report.pdf"]
    for where in copies:
        touch(base / "dupes" / where, content)
    # Same name and size, different content: not duplicates.
    touch(base / "dupes" / "a" / "config.ini", b"value=1")
    touch(base / "dupes" / "b" / "config.ini", b"value=2")
    # A duplicated folder.
    for copy in ["album", "album (copy)"]:
        for index in range(20):
            touch(base / "dupes" / copy / f"photo{index:02d}.jpg", bytes([index]) * 32)


def large_duplicates(base: Path, rng: random.Random) -> None:
    """A few MB: real copies, near-copies that only a full read tells apart, a hard link."""
    clip = rng.randbytes(2 * 1024 * 1024)
    for where in ["videos/clip.mp4", "backup/old/clip.mp4", "Downloads/clip (1).mp4"]:
        touch(base / "dupes" / where, clip)
    # Same size, different last bytes: told apart by the partial digest.
    touch(base / "dupes" / "near" / "a.bin", clip[:-1] + b"A")
    touch(base / "dupes" / "near" / "b.bin", clip[:-1] + b"B")
    # Same first and last 64 KiB, different middle: only the full digest tells.
    middle = rng.randbytes(1024 * 1024)
    touch(base / "dupes" / "middle" / "a.bin", middle)
    touch(base / "dupes" / "middle" / "b.bin", middle[:500_000] + b"X" + middle[500_001:])
    # Two names for one inode: not a duplicate.
    touch(base / "dupes" / "hard" / "original.dat", rng.randbytes(50_000))
    os.link(base / "dupes" / "hard" / "original.dat", base / "dupes" / "hard" / "alias.dat")


def micro_duplicates(base: Path) -> None:
    """Many tiny identical files and empty files, some of them protected."""
    for index in range(300):
        touch(base / "micro" / f"dir{index:03d}" / "note.txt", b"hello\n")
    for index in range(100):
        touch(base / "micro" / "logs" / f"empty{index:03d}.log")
    repo = base / "micro" / "repo"
    touch(repo / ".git" / "HEAD", b"ref: refs/heads/main\n")
    touch(repo / "src" / "__init__.py")
    touch(repo / "data" / "sample.txt", b"hello\n")  # same content, but inside a repository
    touch(repo / "empty.log")


def heavy_files(base: Path, rng: random.Random, megabytes: int) -> None:
    """Real-size files to load the duplicate search: few distinct sizes, so many
    files share a size without being identical; about half are true copies."""
    sizes = [rng.randint(64, 4096) * 1024 for _ in range(24)]
    originals: dict[int, bytes] = {}
    written = 0
    index = 0
    while written < megabytes * 1024 * 1024:
        size = rng.choice(sizes)
        if size in originals and rng.random() < 0.5:
            content = originals[size]  # a true duplicate
        else:
            content = rng.randbytes(size)  # same size, different content
            originals.setdefault(size, content)
        touch(base / "heavy" / f"set{index % 97:02d}" / f"file{index:05d}.bin", content)
        written += size
        index += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument(
        "--heavy-mb", type=int, default=0, help="add real-size files for the duplicate search"
    )
    args = parser.parse_args()

    target: Path = args.target
    if target.exists() and any(target.iterdir()):
        print(f"refusing to write into a non-empty directory: {target}", file=sys.stderr)
        return 2
    target.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)  # noqa: S311 - test data, not cryptography
    steam_like_library(target / "Steam", rng, args.scale)
    wide_directory(target / "Steam", args.scale)
    deep_directory(target)
    odd_names(target)
    links_and_specials(target)
    parasite_directories(target)
    duplicates(target)
    large_duplicates(target, rng)
    micro_duplicates(target)
    if args.heavy_mb:
        heavy_files(target, rng, args.heavy_mb)
    print(f"dummy tree written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
