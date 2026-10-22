"""Verified archives (docs/DECISIONS.md D-024, D-032): writing, verification,
publication, and every way an archive can go wrong."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
import tarfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from treeaudit.archive import destination, runner
from treeaudit.archive.destination import ArchiveDirReason, ArchiveDirRejectedError
from treeaudit.archive.output import ArchiveOutput, Compression, OutputError, TarStream
from treeaudit.archive.runner import (
    ArchiveOutcome,
    ArchiveProgress,
    ArchiveRequest,
    ArchiveStatus,
    InsufficientSpaceError,
    archive_base_name,
    estimate_needed_space,
    execute_archive,
    same_disk,
)
from treeaudit.archive.source import HashingReader
from treeaudit.archive.verify import (
    KIND_DIR,
    KIND_FILE,
    KIND_SYMLINK,
    Manifest,
    VerifyResult,
    fingerprint,
    printable,
    verify_archive,
)
from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.core.datadir import ArchiveLock, ArchiveLockedError
from treeaudit.roots.mountinfo import MountTable, parse_mountinfo
from treeaudit.roots.personal import personal_root
from treeaudit.roots.validation import RootRejectedError
from treeaudit.scanner.walker import SubpathError

non_root_only = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")

NOW = datetime(2026, 10, 21, 14, 30, 5)
BIG = bytes(range(256)) * 5000  # 1.28 MB: more than one read block
WEIRD_NAME = b"caf\xe9-latin1\nnewline.txt"  # invalid UTF-8 and a control character
LONG_NAME = "n" * 150 + ".txt"  # longer than the 100 bytes of a classic tar header


@pytest.fixture(autouse=True)
def tmp_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's tmp_path lives below /tmp, refused for archives: lift that one rule."""
    trees = tuple(tree for tree in destination.SYSTEM_TREES if tree != "/tmp")
    monkeypatch.setattr(destination, "SYSTEM_TREES", trees)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "Backup 2019"
    (root / "docs" / "old").mkdir(parents=True)
    (root / "docs" / "report.pdf").write_bytes(b"p" * 4000)
    (root / "docs" / "old" / "notes.txt").write_bytes(b"secret notes")
    (root / "docs" / "empty").write_bytes(b"")
    (root / "big.bin").write_bytes(BIG)
    (root / "script.sh").write_bytes(b"#!/bin/sh\n")
    (root / "script.sh").chmod(0o750)
    (root / LONG_NAME).write_bytes(b"long")
    (root / os.fsdecode(WEIRD_NAME)).write_bytes(b"weird")
    (root / "to-passwd").symlink_to("/etc/passwd")
    (root / "dangling").symlink_to("nowhere/at/all")
    (root / "empty-dir").mkdir()
    os.utime(root / "docs" / "report.pdf", ns=(1_600_000_000_123_456_789,) * 2)
    return root


@pytest.fixture
def archives(tmp_path: Path) -> Path:
    path = tmp_path / "archives"
    path.mkdir()
    return path


def mounts_for(tmp_path: Path, archives_on_other_disk: bool = False) -> MountTable:
    """System on 259:3; tmp_path on a data disk; optionally archives on a third disk."""
    lines = [
        "1 0 259:3 / / rw,relatime - ext4 /dev/nvme0n1p3 rw",
        f"2 1 8:1 / {tmp_path} rw,relatime - ext4 /dev/sdz1 rw",
    ]
    if archives_on_other_disk:
        lines.append(f"3 2 8:17 / {tmp_path / 'archives'} rw,relatime - ext4 /dev/sdb1 rw")
    return parse_mountinfo(("\n".join(lines) + "\n").encode())


def config_for(tmp_path: Path, source: Path, archives: Path | None) -> AppConfig:
    return AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(source), label="main"),),
        archive_dir=None if archives is None else str(archives),
    )


def run(
    tmp_path: Path,
    source: Path,
    archives: Path | None,
    *,
    subpath: str = "",
    compression: Compression = Compression.GZ,
    cancel: threading.Event | None = None,
    progress: ArchiveProgress | None = None,
    other_disk: bool = False,
) -> ArchiveOutcome:
    config = config_for(tmp_path, source, archives)
    return execute_archive(
        config,
        ArchiveRequest(root=config.roots[0], subpath=subpath, compression=compression),
        mounts=mounts_for(tmp_path, other_disk),
        home=None,
        cancel=cancel or threading.Event(),
        progress=progress,
        now=NOW,
    )


def snapshot(root: Path) -> dict[bytes, tuple[int, int, int, bytes]]:
    """Everything an archive must never change in the source."""
    result: dict[bytes, tuple[int, int, int, bytes]] = {}
    for dirpath, dirnames, filenames in os.walk(os.fsencode(root)):
        for name in [*dirnames, *filenames]:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            content = b""
            if stat.S_ISREG(st.st_mode):
                content = hashlib.sha256(Path(os.fsdecode(path)).read_bytes()).digest()
            result[path] = (st.st_mode, st.st_size, st.st_mtime_ns, content)
    return result


def members(path: Path) -> dict[bytes, tarfile.TarInfo]:
    with tarfile.open(path, encoding="utf-8", errors="surrogateescape") as tar:
        return {os.fsencode(m.name): m for m in tar.getmembers()}


def member_bytes(path: Path, name: bytes) -> bytes:
    with tarfile.open(path, encoding="utf-8", errors="surrogateescape") as tar:
        handle = tar.extractfile(os.fsdecode(name))
        assert handle is not None
        return handle.read()


# --- A complete archive --------------------------------------------------------


@pytest.mark.parametrize("compression", list(Compression))
def test_complete_archive(
    tmp_path: Path, source: Path, archives: Path, compression: Compression
) -> None:
    before = snapshot(source)
    progress = ArchiveProgress()
    outcome = run(tmp_path, source, archives, compression=compression, progress=progress)

    assert outcome.status is ArchiveStatus.COMPLETE, outcome
    assert outcome.verified
    assert outcome.problem_count == 0
    expected_name = f"Backup_2019-20261021-143005.tar.{compression.value}"
    assert outcome.archive_name == expected_name
    assert outcome.archive_path_shown == f"{archives}/{expected_name}"
    assert os.listdir(archives) == [expected_name], "no temporary file may remain"
    archive = archives / expected_name
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert outcome.archive_size == archive.stat().st_size
    assert progress.stage == "done"
    assert progress.bytes_read == progress.bytes_total == outcome.bytes_read

    found = members(archive)
    top = b"Backup 2019"
    assert set(found) == {
        top,
        top + b"/docs",
        top + b"/docs/old",
        top + b"/docs/report.pdf",
        top + b"/docs/old/notes.txt",
        top + b"/docs/empty",
        top + b"/big.bin",
        top + b"/script.sh",
        top + b"/" + LONG_NAME.encode(),
        top + b"/" + WEIRD_NAME,
        top + b"/to-passwd",
        top + b"/dangling",
        top + b"/empty-dir",
    }
    assert outcome.entries == len(found)
    assert member_bytes(archive, top + b"/big.bin") == BIG
    assert member_bytes(archive, top + b"/" + WEIRD_NAME) == b"weird"
    assert member_bytes(archive, top + b"/docs/empty") == b""
    # Symlinks are archived as links, never followed (/etc/passwd is not read).
    link = found[top + b"/to-passwd"]
    assert link.issym() and link.linkname == "/etc/passwd" and link.size == 0
    assert found[top + b"/dangling"].linkname == "nowhere/at/all"
    assert found[top + b"/script.sh"].mode == 0o750
    assert found[top + b"/docs"].isdir()
    assert found[top + b"/docs/report.pdf"].mtime == 1_600_000_000  # whole seconds

    assert_depth_first(archive)
    assert_safe_to_extract(archive)
    assert snapshot(source) == before, "the archived folder must never change"


def assert_safe_to_extract(path: Path) -> None:
    """No member can be written outside the extraction folder: relative names,
    no '..', and nothing below a symlink member (the classic tar link attack)."""
    with tarfile.open(path, encoding="utf-8", errors="surrogateescape") as tar:
        found = tar.getmembers()
    links = {os.fsencode(m.name) for m in found if m.issym() or m.islnk()}
    for member in found:
        name = os.fsencode(member.name)
        parts = name.split(b"/")
        assert not name.startswith(b"/") and b".." not in parts and b"" not in parts, name
        ancestors = {b"/".join(parts[:i]) for i in range(1, len(parts))}
        assert not ancestors & links, name
    # The data filter of Python's tarfile (PEP 706) accepts every member.
    with tarfile.open(path, encoding="utf-8", errors="surrogateescape") as tar:
        for member in tar.getmembers():
            if not member.issym():  # absolute link targets are kept as links, never followed
                tarfile.data_filter(member, "/extract")


def assert_depth_first(path: Path) -> None:
    """Each directory is immediately followed by its whole content, as tar expects."""
    with tarfile.open(path, encoding="utf-8", errors="surrogateescape") as tar:
        names = [os.fsencode(m.name) for m in tar.getmembers()]
    for index, name in enumerate(names):
        inside = [i for i, other in enumerate(names) if other.startswith(name + b"/")]
        assert inside == list(range(index + 1, index + 1 + len(inside))), name


def test_subfolder_archive(tmp_path: Path, source: Path, archives: Path) -> None:
    outcome = run(tmp_path, source, archives, subpath="docs/old")
    assert outcome.status is ArchiveStatus.COMPLETE
    assert outcome.archive_name == "old-20261021-143005.tar.gz"
    assert set(members(archives / outcome.archive_name)) == {b"old", b"old/notes.txt"}


def test_same_disk_warning(tmp_path: Path, source: Path, archives: Path) -> None:
    assert run(tmp_path, source, archives).same_disk
    assert not run(tmp_path, source, archives, other_disk=True).same_disk


def test_existing_name_is_never_replaced(tmp_path: Path, source: Path, archives: Path) -> None:
    taken = archives / "Backup_2019-20261021-143005.tar.gz"
    taken.write_bytes(b"precious")
    outcome = run(tmp_path, source, archives)
    assert outcome.archive_name == "Backup_2019-20261021-143005-2.tar.gz"
    assert taken.read_bytes() == b"precious"


def test_publication_without_hard_links(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exFAT or FAT USB keys: link(2) is refused, the archive is renamed instead."""

    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    taken = archives / "Backup_2019-20261021-143005.tar.gz"
    taken.write_bytes(b"precious")
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.COMPLETE
    assert outcome.archive_name == "Backup_2019-20261021-143005-2.tar.gz"
    assert taken.read_bytes() == b"precious"
    assert set(os.listdir(archives)) == {taken.name, outcome.archive_name}


# --- Incomplete archives -----------------------------------------------------------


def test_special_file_makes_the_archive_incomplete(
    tmp_path: Path, source: Path, archives: Path
) -> None:
    os.mkfifo(source / "pipe")
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.INCOMPLETE
    assert outcome.verified
    assert outcome.archive_name == "Backup_2019-20261021-143005-INCOMPLET.tar.gz"
    assert [(p.path, "spécial" in p.message) for p in outcome.problems] == [("pipe", True)]
    assert b"Backup 2019/pipe" not in members(archives / outcome.archive_name)


@non_root_only
def test_unreadable_file_and_folder(tmp_path: Path, source: Path, archives: Path) -> None:
    (source / "docs" / "report.pdf").chmod(0o000)
    (source / "docs" / "old").chmod(0o000)
    try:
        outcome = run(tmp_path, source, archives)
    finally:
        (source / "docs" / "report.pdf").chmod(0o644)
        (source / "docs" / "old").chmod(0o755)
    assert outcome.status is ArchiveStatus.INCOMPLETE
    assert {p.path for p in outcome.problems} == {"docs/report.pdf", "docs/old"}
    assert all("illisible" in p.message for p in outcome.problems)
    found = members(archives / str(outcome.archive_name))
    assert found[b"Backup 2019/docs/old"].isdir(), "an unreadable folder is kept, empty"
    assert b"Backup 2019/docs/old/notes.txt" not in found
    assert_depth_first(archives / str(outcome.archive_name))


def test_nested_mount_point_is_reported(tmp_path: Path, source: Path, archives: Path) -> None:
    config = config_for(tmp_path, source, archives)
    point = str(source / "docs").replace(" ", "\\040")  # mountinfo escapes spaces
    line = f"9 2 8:33 / {point} rw - ext4 /dev/sdc1 rw\n"
    table = mounts_for(tmp_path).entries + parse_mountinfo(line.encode()).entries
    outcome = execute_archive(
        config,
        ArchiveRequest(root=config.roots[0]),
        mounts=MountTable(entries=table),
        home=None,
        cancel=threading.Event(),
        now=NOW,
    )
    assert outcome.status is ArchiveStatus.INCOMPLETE
    assert [p.path for p in outcome.problems] == ["docs"]
    assert "autre disque" in outcome.problems[0].message


class _Hooked(HashingReader):
    """HashingReader running a callback after its first read of a given file."""

    target = b""
    hook: Callable[[], None] = staticmethod(lambda: None)

    def __init__(self, fd: int, size: int, **kwargs: object) -> None:
        super().__init__(fd, size, **kwargs)  # type: ignore[arg-type]
        self._hooked = os.readlink(f"/proc/self/fd/{fd}").endswith(os.fsdecode(self.target))

    def read(self, size: int = -1) -> bytes:
        data = super().read(size)
        if self._hooked:
            self._hooked = False
            type(self).hook()
        return data


def hook_reader(monkeypatch: pytest.MonkeyPatch, target: str, hook: Callable[[], None]) -> None:
    monkeypatch.setattr(_Hooked, "target", os.fsencode(target))
    monkeypatch.setattr(_Hooked, "hook", staticmethod(hook))
    monkeypatch.setattr(runner, "HashingReader", _Hooked)
    monkeypatch.setattr("treeaudit.archive.source.BLOCK_SIZE", 64 * 1024)


def test_file_growing_while_read(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def append() -> None:
        with (source / "big.bin").open("ab") as handle:
            handle.write(b"more")

    hook_reader(monkeypatch, "big.bin", append)
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.INCOMPLETE
    assert outcome.verified
    assert [(p.path, p.message) for p in outcome.problems] == [
        ("big.bin", "modifié pendant la lecture : copie incertaine")
    ]


def test_file_shrinking_while_read_keeps_the_archive_readable(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = source / "big.bin"
    hook_reader(monkeypatch, "big.bin", lambda: os.truncate(big, 100))
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.INCOMPLETE
    assert outcome.verified, "the archive stays consistent and passes verification"
    assert [p.path for p in outcome.problems] == ["big.bin"]
    assert "raccourci" in outcome.problems[0].message
    data = member_bytes(archives / str(outcome.archive_name), b"Backup 2019/big.bin")
    assert len(data) == len(BIG)  # padded with zeros to the declared size


def test_file_changed_since_the_first_walk(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measure = runner._ArchiveJob._measure

    def measure_then_modify(self: runner._ArchiveJob) -> bool:
        result = measure(self)
        (source / "docs" / "report.pdf").write_bytes(b"changed")
        return result

    monkeypatch.setattr(runner._ArchiveJob, "_measure", measure_then_modify)
    outcome = run(tmp_path, source, archives)
    # The second walk sees the new size: the file is archived as it is now.
    assert outcome.status is ArchiveStatus.COMPLETE
    data = member_bytes(archives / str(outcome.archive_name), b"Backup 2019/docs/report.pdf")
    assert data == b"changed"


# --- Failures and cancellation: nothing stays behind ----------------------------


def test_cancelled_before_writing(tmp_path: Path, source: Path, archives: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    outcome = run(tmp_path, source, archives, cancel=cancel)
    assert outcome.status is ArchiveStatus.CANCELLED
    assert os.listdir(archives) == []


def test_cancelled_while_writing(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = threading.Event()
    hook_reader(monkeypatch, "big.bin", cancel.set)
    outcome = run(tmp_path, source, archives, cancel=cancel)
    assert outcome.status is ArchiveStatus.CANCELLED
    assert outcome.archive_name is None
    assert os.listdir(archives) == [], "the temporary file must be removed"


def test_cancelled_while_verifying(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = threading.Event()
    original = runner.verify_archive

    def verify_after_cancel(*args: object, **kwargs: object) -> VerifyResult:
        cancel.set()
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "verify_archive", verify_after_cancel)
    outcome = run(tmp_path, source, archives, cancel=cancel)
    assert outcome.status is ArchiveStatus.CANCELLED
    assert os.listdir(archives) == []


def test_failed_verification_removes_the_archive(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = ArchiveOutput.sync

    def sync_then_corrupt(self: ArchiveOutput) -> int:
        size = sync(self)
        partial = next(archives.iterdir())
        data = bytearray(partial.read_bytes())
        data[len(data) // 2] ^= 0xFF  # one flipped byte in the compressed data
        partial.write_bytes(bytes(data))
        return size

    monkeypatch.setattr(ArchiveOutput, "sync", sync_then_corrupt)
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.FAILED
    assert not outcome.verified
    assert outcome.failure is not None and outcome.failure.startswith("vérification échouée")
    assert os.listdir(archives) == []


def test_write_error_fails_and_cleans_up(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(self: ArchiveOutput) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ArchiveOutput, "sync", disk_full)
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and "No space left" in outcome.failure
    assert os.listdir(archives) == []


def test_insufficient_space_is_refused_before_writing(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ArchiveOutput, "free_bytes", lambda self: 1000)
    with pytest.raises(InsufficientSpaceError) as caught:
        run(tmp_path, source, archives)
    assert caught.value.available == 1000
    assert caught.value.needed > len(BIG)
    assert os.listdir(archives) == []


def test_archive_dir_inside_the_folder_is_refused_before_writing(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Possible when the archive directory is on a detected external disk (D-033)."""
    inside = source / "docs" / "archives"
    inside.mkdir()
    monkeypatch.setattr(runner, "validate_archive_dir", lambda path, ctx: None)
    outcome = run(tmp_path, source, inside)
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and "contient TreeAudit lui-même" in outcome.failure
    assert os.listdir(inside) == []
    # Archiving a sibling folder of the same disk is fine.
    assert run(tmp_path, source, inside, subpath="docs/old").status is ArchiveStatus.COMPLETE


def test_archive_dir_inside_the_folder_is_caught_while_writing(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth, should both earlier checks ever miss an overlap."""
    inside = source / "docs" / "archives"
    inside.mkdir()
    monkeypatch.setattr(runner, "validate_archive_dir", lambda path, ctx: None)
    monkeypatch.setattr(runner._ArchiveJob, "_measure", lambda self: True)
    outcome = run(tmp_path, source, inside)
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and "inside the archived folder" in outcome.failure
    assert os.listdir(inside) == []


# --- Refusals before anything is written --------------------------------------------


def test_refusals(tmp_path: Path, source: Path, archives: Path) -> None:
    with pytest.raises(ArchiveDirRejectedError) as caught:
        run(tmp_path, source, None)
    assert caught.value.reason is ArchiveDirReason.NOT_CONFIGURED
    with pytest.raises(ArchiveDirRejectedError) as caught:
        run(tmp_path, source, source / "docs")
    assert caught.value.reason is ArchiveDirReason.ROOT_OVERLAP
    with pytest.raises(SubpathError):
        run(tmp_path, source, archives, subpath="../etc")
    with pytest.raises(RootRejectedError):
        run(tmp_path, tmp_path / "missing", archives)
    assert os.listdir(archives) == []


def test_missing_subfolder_fails_without_writing(
    tmp_path: Path, source: Path, archives: Path
) -> None:
    outcome = run(tmp_path, source, archives, subpath="nope")
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and outcome.failure.startswith("nope")
    assert os.listdir(archives) == []


def test_one_archive_at_a_time(tmp_path: Path, source: Path, archives: Path) -> None:
    (tmp_path / "data").mkdir(mode=0o700)
    with ArchiveLock(str(tmp_path / "data")), pytest.raises(ArchiveLockedError):
        run(tmp_path, source, archives)


# --- Output: the only code that writes ------------------------------------------------


def test_output_creates_only_new_files(archives: Path) -> None:
    (archives / "taken.partial").write_bytes(b"precious")
    with ArchiveOutput(str(archives)) as output, pytest.raises(FileExistsError):
        output.create("taken.partial")
    assert (archives / "taken.partial").read_bytes() == b"precious"


def test_output_never_follows_a_symlink(archives: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    (archives / "trap.partial").symlink_to(target)
    with ArchiveOutput(str(archives)) as output, pytest.raises(OSError):
        output.create("trap.partial")
    assert not target.exists()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "x\x00y", "n" * 256])
def test_output_refuses_odd_names(archives: Path, name: str) -> None:
    with ArchiveOutput(str(archives)) as output, pytest.raises(OutputError):
        output.create(name)


def test_discard_removes_only_its_own_file(archives: Path) -> None:
    with ArchiveOutput(str(archives)) as output:
        output.create(".a.partial").write(b"data")
        output.close_file()
        # Replaced behind TreeAudit's back: the new file is not ours, keep it.
        (archives / ".a.partial").unlink()
        (archives / ".a.partial").write_bytes(b"someone else's")
        assert output.discard() is False
    assert (archives / ".a.partial").read_bytes() == b"someone else's"

    with ArchiveOutput(str(archives)) as output:
        output.create(".b.partial")
        assert output.discard() is True
        assert output.discard() is False
    assert sorted(os.listdir(archives)) == [".a.partial"]


def test_publish_refuses_a_replaced_temporary_file(archives: Path) -> None:
    with ArchiveOutput(str(archives)) as output:
        output.create(".c.partial")
        output.close_file()
        (archives / ".c.partial").unlink()
        (archives / ".c.partial").write_bytes(b"other")
        with pytest.raises(OutputError):
            output.publish("c.tar.gz")
    assert sorted(os.listdir(archives)) == [".c.partial"]


# --- Verification on its own ---------------------------------------------------------------


def small_archive(entries: list[tuple[str, bytes]]) -> tuple[bytes, Manifest]:
    buffer = io.BytesIO()
    manifest = Manifest()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
            digest = hashlib.sha256(content).digest()
            manifest.add(fingerprint(KIND_FILE, name.encode(), 0o644, len(content), b"", digest))
    return buffer.getvalue(), manifest


def verify_bytes(tmp_path: Path, data: bytes, manifest: Manifest) -> VerifyResult:
    path = tmp_path / "check.tar.gz"
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY)
    try:
        return verify_archive(
            fd, Compression.GZ, manifest, cancel=threading.Event(), on_read=lambda n: None
        )
    finally:
        os.close(fd)


def test_verify_accepts_a_matching_archive(tmp_path: Path) -> None:
    data, manifest = small_archive([("a", b"one"), ("b", b"two")])
    assert verify_bytes(tmp_path, data, manifest) == VerifyResult(True, 2)


def test_verify_detects_differences(tmp_path: Path) -> None:
    data, manifest = small_archive([("a", b"one"), ("b", b"two")])
    other, _ = small_archive([("a", b"one"), ("b", b"TWO")])
    result = verify_bytes(tmp_path, other, manifest)
    assert not result.ok and "n° 2 (b)" in str(result.problem)

    _, shorter = small_archive([("a", b"one")])
    assert "en trop" in str(verify_bytes(tmp_path, data, shorter).problem)
    single, _ = small_archive([("a", b"one")])
    assert "manquant" in str(verify_bytes(tmp_path, single, manifest).problem)

    assert "illisible" in str(verify_bytes(tmp_path, data[:-20], manifest).problem)
    corrupted = bytearray(data)
    corrupted[-6] ^= 0xFF  # gzip trailer (CRC-32 / size)
    assert not verify_bytes(tmp_path, bytes(corrupted), manifest).ok


def test_fingerprints_are_unambiguous() -> None:
    assert fingerprint(KIND_FILE, b"ab", 0o644, 1, b"", b"c") != fingerprint(
        KIND_FILE, b"a", 0o644, 1, b"", b"bc"
    )
    assert fingerprint(KIND_DIR, b"x", 0o755, 0, b"", b"") != fingerprint(
        KIND_SYMLINK, b"x", 0o755, 0, b"", b""
    )
    assert fingerprint(KIND_FILE, b"x", 0o644, 0, b"", b"") != fingerprint(
        KIND_FILE, b"x", 0o600, 0, b"", b""
    )


# --- Small helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"Backup 2019", "Backup_2019"),
        (b"..hidden", "hidden"),
        (b"-rf", "rf"),
        (b"$(id)", "id"),
        (b"photos d'\xc3\xa9t\xc3\xa9", "photos_d_t"),
        (b"\xff\xfe", "archive"),
        (b"...", "archive"),
        (b"x" * 200, "x" * 80),
    ],
)
def test_archive_base_name(raw: bytes, expected: str) -> None:
    assert archive_base_name(raw) == expected


def test_printable_escapes_control_characters() -> None:
    assert printable(WEIRD_NAME) == "caf\\xe9-latin1\\x0anewline.txt"


def test_estimate_is_a_worst_case() -> None:
    assert estimate_needed_space(10**9, 1000) > 10**9 + 1000 * 512


def test_same_disk_detection() -> None:
    table = parse_mountinfo(
        b"1 0 259:3 / / rw - ext4 /dev/nvme0n1p3 rw\n"
        b"2 1 259:4 / /mnt/a rw - ext4 /dev/nvme0n1p4 rw\n"
        b"3 1 8:1 / /mnt/b rw - ext4 /dev/sda1 rw\n"
        b"4 1 8:2 / /mnt/c rw - ext4 /dev/sda2 rw\n"
        b"5 1 253:0 / /mnt/d rw - ext4 /dev/mapper/luks-1 rw\n"
    )
    assert same_disk(table, "/mnt/a/x", "/mnt/a/y")  # same filesystem
    assert same_disk(table, "/mnt/a", "/home")  # two partitions of nvme0n1
    assert same_disk(table, "/mnt/b", "/mnt/c")  # sda1 and sda2
    assert not same_disk(table, "/mnt/a", "/mnt/b")
    assert not same_disk(table, "/mnt/d", "/mnt/b")  # unknown: no false alarm


# --- The tar writer --------------------------------------------------------------------------


def test_tar_stream_is_a_standard_tar() -> None:
    buffer = io.BytesIO()
    with TarStream(buffer) as tar:  # type: ignore[arg-type]
        folder = tarfile.TarInfo("dir")
        folder.type = tarfile.DIRTYPE
        tar.add(folder)
        for name, content in (("dir/a", b"x" * 513), (LONG_NAME, b""), ("caf\udce9", b"e")):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.add(info, io.BytesIO(content))
    data = buffer.getvalue()
    assert len(data) % tarfile.RECORDSIZE == 0
    with tarfile.open(
        fileobj=io.BytesIO(data), encoding="utf-8", errors="surrogateescape"
    ) as reader:
        found = {m.name: m for m in reader.getmembers()}
        assert set(found) == {"dir", "dir/a", LONG_NAME, "caf\udce9"}
        handle = reader.extractfile("dir/a")
        assert handle is not None and handle.read() == b"x" * 513


def test_tar_stream_refuses_a_size_mismatch() -> None:
    info = tarfile.TarInfo("a")
    info.size = 10
    with pytest.raises(OutputError):
        TarStream(io.BytesIO()).add(info, io.BytesIO(b"short"))  # type: ignore[arg-type]
    with pytest.raises(OutputError):
        TarStream(io.BytesIO()).add(info)  # type: ignore[arg-type]


# --- Security review (step d) ------------------------------------------------------------


def test_full_disk_still_removes_the_temporary_file(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a full disk, closing the file fails too: the removal must still happen."""

    close_compressor = ArchiveOutput._close_compressor

    def disk_full(self: ArchiveOutput) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    def close_then_fail(self: ArchiveOutput) -> None:
        was_open = self._compressor is not None
        close_compressor(self)  # gzip marks itself closed even when its last write fails
        if was_open:
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ArchiveOutput, "sync", disk_full)
    monkeypatch.setattr(ArchiveOutput, "_close_compressor", close_then_fail)
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.FAILED
    assert os.listdir(archives) == []


def test_published_name_must_be_the_verified_archive(
    tmp_path: Path, source: Path, archives: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the final name ends up designating another file, nothing is claimed."""

    def swapped_link(src: str, dst: str, **kwargs: object) -> None:
        (archives / dst).write_bytes(b"not the archive")

    monkeypatch.setattr(os, "link", swapped_link)
    outcome = run(tmp_path, source, archives)
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.archive_name is None
    assert outcome.failure is not None and "does not designate" in outcome.failure
    # The temporary archive is removed; the foreign file is left alone.
    assert os.listdir(archives) == ["Backup_2019-20261021-143005.tar.gz"]
    assert (archives / "Backup_2019-20261021-143005.tar.gz").read_bytes() == b"not the archive"


def test_folder_chosen_in_an_analysis_must_still_be_the_same(
    tmp_path: Path, source: Path, archives: Path
) -> None:
    config = config_for(tmp_path, source, archives)
    docs = os.lstat(source / "docs")

    def archive(expected: tuple[int, int]) -> ArchiveOutcome:
        request = ArchiveRequest(root=config.roots[0], subpath="docs", expected_folder=expected)
        return execute_archive(
            config,
            request,
            mounts=mounts_for(tmp_path),
            home=None,
            cancel=threading.Event(),
            now=NOW,
        )

    assert archive((docs.st_dev, docs.st_ino)).status is ArchiveStatus.COMPLETE
    outcome = archive((docs.st_dev, docs.st_ino + 1))
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and "n'est plus celui de l'analyse" in outcome.failure
    assert len(os.listdir(archives)) == 1


# --- TreeAudit's own folders and private folders (D-037) ------------------------------------


def run_root(tmp_path: Path, root: RootConfig, archives: Path, data_dir: Path) -> ArchiveOutcome:
    config = AppConfig(data_dir=str(data_dir), roots=(root,), archive_dir=str(archives))
    return execute_archive(
        config,
        ArchiveRequest(root=root),
        mounts=mounts_for(tmp_path),
        home=None,
        cancel=threading.Event(),
        now=NOW,
    )


def test_a_folder_holding_the_data_dir_is_refused_before_writing(
    tmp_path: Path, archives: Path
) -> None:
    disk = tmp_path / "disk"
    data = disk / "tools" / "treeaudit-data"
    data.mkdir(parents=True)
    (disk / "photo.jpg").write_bytes(b"j")
    root = RootConfig(id="ext-disk-000000", path=str(disk), label="Disk", external=True)
    outcome = run_root(tmp_path, root, archives, data)
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and "contient TreeAudit lui-même" in outcome.failure
    assert os.listdir(archives) == []


def test_a_folder_holding_private_folders_is_refused_but_its_subfolders_are_not(
    tmp_path: Path, archives: Path
) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_bytes(b"secret")
    (home / "Documents").mkdir()
    (home / "Documents" / "cv.pdf").write_bytes(b"cv")
    root = personal_root(str(home))
    outcome = run_root(tmp_path, root, archives, tmp_path / "data")
    assert outcome.status is ArchiveStatus.FAILED
    assert outcome.failure is not None and "dossiers privés" in outcome.failure
    assert os.listdir(archives) == []
    config = AppConfig(data_dir=str(tmp_path / "data"), roots=(root,), archive_dir=str(archives))
    documents = execute_archive(
        config,
        ArchiveRequest(root=root, subpath="Documents"),
        mounts=mounts_for(tmp_path),
        home=None,
        cancel=threading.Event(),
        now=NOW,
    )
    assert documents.status is ArchiveStatus.COMPLETE
