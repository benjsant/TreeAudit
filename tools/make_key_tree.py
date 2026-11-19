"""Generate a test tree on a USB key, with a sheet of the results TreeAudit must find.

    python3 tools/make_key_tree.py <empty folder on the key> --manifest <file off the key>

Made for the tests on real file systems (exFAT, NTFS, btrfs). Unlike
make_dummy_tree.py it never stops on what a file system refuses (a link on
exFAT, a name forbidden by Windows...): each refusal is written in the sheet,
which then tells exactly how the file system behaves. The sheet also gives the
totals and duplicate groups of the generated folder, counted while writing it,
to compare figure by figure with what TreeAudit shows.

The tree holds:
- enough small files for a scan that lasts, and big random files (with true
  copies) for a duplicate search that reads for a while: a key can then be
  pulled out during either;
- duplicates with a known answer, and look-alikes that are not duplicates;
- names that file systems treat differently;
- links, a fifo and a sparse file, when the file system has them;
- small projects, dated photos and two nearly identical folders.

This is a development tool: it only creates files inside the target folder,
which must not exist yet (or be empty), never removes nor renames anything,
and is deterministic (fixed seeds). Nothing personal: every name and every
byte is invented.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import random
import stat
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEED = 20261119
DUP_MIN_SIZE = 1024  # TreeAudit's default minimum size for the duplicate search
CHUNK = 4 * 1024 * 1024
BIG_FILE_MAX = 250 * 1024 * 1024
DEFAULT_SMALL_FILES = 20_000
DEFAULT_BIG_MB = 2_500


@dataclass
class KeyTree:
    """Creates entries under ``root`` and counts them the way TreeAudit does:
    every name counts in the numbers, a hard-linked inode's size counts once
    (D-022). Folders made by the tree only; the root itself is not counted."""

    root: Path
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    others: int = 0
    logical_size: int = 0
    hardlinked_inodes: int = 0
    refused: list[dict[str, str]] = field(default_factory=list)
    _made_dirs: set[str] = field(default_factory=set)
    _created: list[bytes] = field(default_factory=list)
    _digests: dict[str, bytes] = field(default_factory=dict)
    """Single-name regular files of at least DUP_MIN_SIZE bytes: path -> digest."""

    def _path(self, rel: str | bytes) -> bytes:
        rel_bytes = os.fsencode(rel)
        return os.fsencode(self.root) + b"/" + rel_bytes

    def _refuse(self, what: str, rel: str | bytes, error: OSError) -> None:
        name = os.fsdecode(rel) if isinstance(rel, bytes) else rel
        code = errno.errorcode.get(error.errno or 0, str(error.errno))
        self.refused.append({"what": what, "path": name, "error": code})

    def mkdir(self, rel: str) -> bool:
        """Create a folder and its missing parents; False if refused."""
        if rel in self._made_dirs:
            return True
        parent = rel.rpartition("/")[0]
        if parent and not self.mkdir(parent):
            return False
        try:
            os.mkdir(self._path(rel), 0o755)
        except OSError as error:
            self._refuse("folder", rel, error)
            return False
        self._made_dirs.add(rel)
        self._created.append(self._path(rel))
        self.dirs += 1
        return True

    def _parent_ready(self, rel: str | bytes) -> bool:
        parent = os.fsdecode(rel).rpartition("/")[0]
        return not parent or self.mkdir(parent)

    def file(self, rel: str | bytes, content: bytes, what: str = "file") -> bool:
        """Create a new file (never overwrite one); False if the name is refused,
        for instance because a name differing only in case already exists."""
        if not self._parent_ready(rel):
            return False
        try:
            fd = os.open(self._path(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as error:
            self._refuse(what, rel, error)
            return False
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        self._add_file(rel, len(content), hashlib.sha256(content).digest())
        return True

    def random_file(self, rel: str, size: int, seed: int) -> bool:
        """A big file of random bytes, written by chunks; the same seed gives
        the same content (a true copy)."""
        if not self._parent_ready(rel):
            return False
        try:
            fd = os.open(self._path(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as error:
            self._refuse("file", rel, error)
            return False
        rng = random.Random(seed)  # noqa: S311 - test data, not cryptography
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as handle:
            left = size
            while left:
                chunk = rng.randbytes(min(CHUNK, left))
                handle.write(chunk)
                digest.update(chunk)
                left -= len(chunk)
        self._add_file(rel, size, digest.digest())
        return True

    def _add_file(self, rel: str | bytes, size: int, digest: bytes) -> None:
        self._created.append(self._path(rel))
        self.files += 1
        self.logical_size += size
        if size >= DUP_MIN_SIZE:
            self._digests[os.fsdecode(rel)] = digest

    def hardlink(self, rel: str, existing: str) -> bool:
        """A second name for an existing file: one more file, no more size."""
        if not self._parent_ready(rel):
            return False
        try:
            os.link(self._path(existing), self._path(rel))
        except OSError as error:
            self._refuse("hard link", rel, error)
            return False
        self.files += 1
        self.hardlinked_inodes += 1
        # Two names for one inode: TreeAudit compares inodes, not names.
        self._digests.pop(existing, None)
        return True

    def symlink(self, rel: str, target: str) -> bool:
        """A symbolic link (never followed by TreeAudit)."""
        if not self._parent_ready(rel):
            return False
        try:
            os.symlink(target, self._path(rel))
        except OSError as error:
            self._refuse("symbolic link", rel, error)
            return False
        self._created.append(self._path(rel))
        self.symlinks += 1
        return True

    def fifo(self, rel: str) -> bool:
        """A named pipe: listed by TreeAudit, never opened."""
        if not self._parent_ready(rel):
            return False
        try:
            os.mkfifo(self._path(rel), 0o644)
        except OSError as error:
            self._refuse("fifo", rel, error)
            return False
        self._created.append(self._path(rel))
        self.others += 1
        return True

    def sparse(self, rel: str, size: int) -> bool:
        """A file with a big logical size and, where the file system allows
        it, almost nothing allocated."""
        if not self._parent_ready(rel):
            return False
        try:
            fd = os.open(self._path(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as error:
            self._refuse("sparse file", rel, error)
            return False
        try:
            os.ftruncate(fd, size)
        finally:
            os.close(fd)
        self._created.append(self._path(rel))
        self.files += 1
        self.logical_size += size
        return True

    def duplicate_groups(self) -> list[list[str]]:
        """Groups of single-name files with the same content, as TreeAudit's
        default search must find them (sorted paths, sorted groups)."""
        by_digest: dict[bytes, list[str]] = defaultdict(list)
        for rel, digest in self._digests.items():
            by_digest[digest].append(rel)
        return sorted(sorted(paths) for paths in by_digest.values() if len(paths) > 1)

    def allocated_size(self) -> int:
        """Space the root and the created entries occupy, measured once everything
        is written. The root's own folder counts, as in TreeAudit's total."""
        seen: set[tuple[int, int]] = set()
        total = 0
        for path in [os.fsencode(self.root), *self._created]:
            st = os.lstat(path)
            if (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
        return total


# --- content ------------------------------------------------------------------------------


def volume(tree: KeyTree, small_files: int) -> None:
    """Many small files, all different, in folders of 200: a scan that lasts."""
    for index in range(small_files):
        rel = f"volume/lot_{index // 200:03d}/fichier_{index:05d}.txt"
        tree.file(rel, f"petit fichier {index}\n".encode() * (1 + index % 20))


def big_files(tree: KeyTree, big_mb: int) -> None:
    """Big random files, each original followed by a true copy: the duplicate
    search must read all of them to the end."""
    total = big_mb * 1024 * 1024
    size = min(BIG_FILE_MAX, total // 2)
    count = max(2, total // size)
    for index in range(count):
        original = index - index % 2
        name = f"film_{original:02d}.mkv" if index % 2 == 0 else f"copie de film_{original:02d}.mkv"
        tree.random_file(f"gros fichiers/{name}", size, SEED + original)


def duplicates(tree: KeyTree) -> None:
    """Duplicates with a known answer, and files that only look like duplicates."""
    rng = random.Random(SEED)  # noqa: S311 - test data, not cryptography
    clip = rng.randbytes(300 * 1024)
    for rel in ["videos/clip.mp4", "sauvegarde/ancien/clip.mp4", "Téléchargements/clip (1).mp4"]:
        tree.file(f"doublons/{rel}", clip)
    # Same size, different last byte: not duplicates.
    tree.file("doublons/presque/a.bin", clip[:-1] + b"A")
    tree.file("doublons/presque/b.bin", clip[:-1] + b"B")
    # Same beginning and end, one byte changed in the middle: not duplicates.
    middle = rng.randbytes(1024 * 1024)
    tree.file("doublons/milieu/a.bin", middle)
    tree.file("doublons/milieu/b.bin", middle[:500_000] + b"X" + middle[500_001:])
    # One file with two names: never a duplicate.
    tree.file("doublons/lien physique/original.dat", rng.randbytes(50_000))
    tree.hardlink("doublons/lien physique/autre nom.dat", "doublons/lien physique/original.dat")
    # A copied folder.
    for copy in ["album", "album (copie)"]:
        for index in range(10):
            tree.file(f"doublons/{copy}/photo{index:02d}.jpg", bytes([index]) * 4096)


def odd_names(tree: KeyTree) -> None:
    """Names that file systems store, refuse or merge differently."""
    names: list[str | bytes] = [
        "avec espace.txt",
        "-commence par un tiret.txt",
        ".caché",
        "émoji 📁.txt",
        "café.txt",  # NFC
        "cafe\u0301.txt",  # NFD (escaped: editors turn it into NFC): another name
        "Photo.JPG",
        "photo.jpg",  # differs only in case: refused where names ignore case
        "a" * 255,  # the longest name everywhere
        "é" * 200 + ".txt",  # 204 characters but 404 bytes
        "nouvelle\nligne.txt",
        "tab\tulation.txt",
        "deux-points:.txt",
        "question?.txt",
        "étoile*.txt",
        'guillemet".txt',
        "chevrons<>.txt",
        "barre|verticale.txt",
        "barre\\inverse.txt",
        "point final.",
        "espace final ",
        "<img src=x onerror=alert(1)>.html",
        "$(echo piégé).txt",
        b"invalide-\xff\xfe-utf8.txt",
    ]
    for index, name in enumerate(names):
        rel = b"noms/" + os.fsencode(name)
        tree.file(rel, f"nom {index}\n".encode(), what="name")


def links_and_specials(tree: KeyTree) -> None:
    """Links, a hard link, a fifo and a sparse file: present only where the
    file system has them, each refusal noted."""
    tree.file("liens/cible.txt", b"cible\n")
    tree.symlink("liens/lien relatif", "cible.txt")
    tree.symlink("liens/vers etc", "/etc")
    tree.symlink("liens/cassé", "n-existe-pas")
    tree.symlink("liens/boucle", ".")
    tree.hardlink("liens/lien physique.txt", "liens/cible.txt")
    tree.fifo("liens/tube")
    tree.sparse("liens/creux-64Mio.img", 64 * 1024 * 1024)


def projects(tree: KeyTree) -> None:
    """Small projects with folders that can be rebuilt (all files under 1 KiB:
    they never enter the duplicate search)."""
    web = "projets/site-web"
    tree.file(f"{web}/package.json", b'{"name": "site-web"}\n')
    tree.file(f"{web}/package-lock.json", b"{}\n")
    tree.file(f"{web}/index.html", b"<!doctype html>\n")
    for index in range(200):
        tree.file(f"{web}/node_modules/dep{index % 20}/f{index}.js", f"// {index}\n".encode())
    py = "projets/outil-python"
    tree.file(f"{py}/pyproject.toml", b'[project]\nname = "outil"\n')
    tree.file(f"{py}/outil/__init__.py", b"")
    tree.file(f"{py}/outil/__pycache__/__init__.cpython-313.pyc", b"\x00" * 16)
    tree.file(f"{py}/.venv/pyvenv.cfg", b"home = /usr/bin\nversion = 3.13.0\n")
    for index in range(100):
        tree.file(f"{py}/.venv/lib/python3.13/site-packages/pkg/m{index}.py", b"")
    tree.file(f"{py}/.git/HEAD", b"ref: refs/heads/main\n")


def photos(tree: KeyTree) -> None:
    """Photos and videos named by date, for « Ranger »; all different."""
    rng = random.Random(SEED + 1)  # noqa: S311 - test data, not cryptography
    for index in range(40):
        year = 2018 + index % 6
        day = f"{year}{1 + index % 12:02d}{1 + index % 28:02d}"
        tree.file(
            f"Photos téléphone/IMG_{day}_{index:06d}.jpg", b"\xff\xd8\xff" + rng.randbytes(2048)
        )
    for index in range(5):
        tree.file(f"Photos téléphone/VID_2021070{index + 1}_120000.mp4", rng.randbytes(4096))
    tree.file("Photos téléphone/Capture d'écran 2022-03-04.png", b"\x89PNG" + rng.randbytes(2048))


def nearly_identical(tree: KeyTree) -> None:
    """Two folders sharing most files: one changed, one added (D-058)."""
    rng = random.Random(SEED + 2)  # noqa: S311 - test data, not cryptography
    shared = {f"photo{index:02d}.jpg": rng.randbytes(5 * 1024) for index in range(8)}
    for folder in ["Vacances 2019", "Vacances 2019 - copie"]:
        for name, content in shared.items():
            tree.file(f"{folder}/{name}", content)
    tree.file("Vacances 2019/notes.txt", b"version 1\n" * 200)
    tree.file("Vacances 2019 - copie/notes.txt", b"version 2\n" * 200)
    tree.file("Vacances 2019 - copie/en plus.jpg", rng.randbytes(5 * 1024))


# --- the sheet ----------------------------------------------------------------------------


def _unescape_mount_field(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def file_system_of(path: Path) -> dict[str, str]:
    """Type, source and options of the mount holding ``path`` (the longest
    mount point that contains it), read from /proc/self/mountinfo."""
    real = os.path.realpath(path)
    best: dict[str, str] = {}
    best_len = -1
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 6:
            continue
        point = _unescape_mount_field(fields[4])
        inside = real == point or real.startswith(point.rstrip("/") + "/")
        if inside and len(point) > best_len:
            fs_type, source, *options = [*right.split(), "", ""]
            best = {"type": fs_type, "source": source, "options": options[0], "mount": point}
            best_len = len(point)
    return best


def generate(target: Path, *, small_files: int, big_mb: int) -> dict[str, Any]:
    """Write the tree in ``target`` (empty or new) and return its sheet."""
    target.mkdir(parents=True, exist_ok=True)
    tree = KeyTree(target)
    volume(tree, small_files)
    duplicates(tree)
    odd_names(tree)
    links_and_specials(tree)
    projects(tree)
    photos(tree)
    nearly_identical(tree)
    big_files(tree, big_mb)
    return {
        "target": str(target),
        "file_system": file_system_of(target),
        "parameters": {"small_files": small_files, "big_mb": big_mb, "seed": SEED},
        "totals": {
            "files": tree.files,
            "dirs": tree.dirs,
            "symlinks": tree.symlinks,
            "others": tree.others,
            "entries": 1 + tree.files + tree.dirs + tree.symlinks + tree.others,
            "logical_size": tree.logical_size,
            "allocated_size": tree.allocated_size(),
        },
        "hardlinked_inodes": tree.hardlinked_inodes,
        "duplicate_groups": tree.duplicate_groups(),
        "duplicate_min_size": DUP_MIN_SIZE,
        "refused": tree.refused,
    }


def _inside(path: Path, folder: Path) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(folder)
    return real == base or real.startswith(base.rstrip("/") + "/")


def main(argv: list[str] | None = None) -> int:
    """Generate the tree and write its sheet; 2 if the target is not empty,
    or if the sheet would land inside the tree or replace a file."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("target", type=Path)
    parser.add_argument("--manifest", type=Path, required=True, help="the sheet, off the key")
    parser.add_argument("--small-files", type=int, default=DEFAULT_SMALL_FILES)
    parser.add_argument("--big-mb", type=int, default=DEFAULT_BIG_MB)
    args = parser.parse_args(argv)

    target: Path = args.target
    manifest: Path = args.manifest
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        print(f"refusing to write into a non-empty directory: {target}", file=sys.stderr)
        return 2
    if _inside(manifest, target):
        print("refusing to write the sheet inside the generated tree", file=sys.stderr)
        return 2
    if manifest.exists() or manifest.is_symlink():
        print(f"refusing to replace an existing file: {manifest}", file=sys.stderr)
        return 2
    target.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(target).st_mode):
        print(f"not a folder: {target}", file=sys.stderr)
        return 2

    sheet = generate(target, small_files=args.small_files, big_mb=args.big_mb)
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(sheet, handle, indent=2)  # escapes undecodable names
        handle.write("\n")
    totals = sheet["totals"]
    print(
        f"{totals['entries']} entries, {totals['logical_size']} bytes written to {target}; "
        f"{len(sheet['refused'])} refused by the file system; sheet: {manifest}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
