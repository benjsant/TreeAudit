"""The double check of proposed commands: never "/", the system, a whole disk."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import pytest

from treeaudit.parasites.rules import RuleError, _check_template
from treeaudit.shellcmd import (
    Action,
    UnsafeTarget,
    check_target,
    cleanup_command,
    verify_command,
)


@dataclass(frozen=True)
class Target:
    rel_path: bytes
    rel_path_display: str


def target(path: str) -> Target:
    return Target(path.encode(), path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/media",
        "/media/disque",  # a whole disk
        "/run/media/moi/disque",
        "/run/media/moi",
        "/mnt/Data",  # a whole partition
        "/home/moi",  # a whole personal folder
        "/var/home/moi",
        "/usr/lib/x",  # the system
        "/etc/passwd",
        "/boot/efi",
        "/root/x",
        "/run/user/1000/x",
        "/data",  # any top-level folder
        "/mnt/Data/a/../..",  # not normalised
        "/mnt/Data/a/./b",
        "/mnt/Data//a",
        "relative/path",
    ],
)
def test_never_a_system_folder_nor_a_whole_disk(path: str) -> None:
    with pytest.raises(UnsafeTarget):
        check_target(path, "/", "/")


def test_never_the_analysed_folder_nor_above_nor_outside() -> None:
    root, scanned = "/media/disque", "/media/disque/Photos"
    for path in ("/media/disque", "/media/disque/Photos", "/media/autre/x", "/media/disqueX/a"):
        with pytest.raises(UnsafeTarget):
            check_target(path, root, scanned)
    check_target("/media/disque/Photos/vieux", root, scanned)


def test_never_a_known_disk_inside_the_root() -> None:
    # /media/moi analysed: /media/moi/USB is a disk TreeAudit knows.
    protected = ("/media/moi", "/media/moi/USB")
    with pytest.raises(UnsafeTarget, match="disque"):
        check_target("/media/moi/USB", "/media/moi", "/media/moi", protected)
    check_target("/media/moi/USB/vieux", "/media/moi", "/media/moi", protected)


@pytest.mark.parametrize("action", list(Action))
def test_the_analysed_disk_is_refused_and_said(action: Action) -> None:
    result = cleanup_command(
        action, [target(""), target("a/node_modules")], "/media/disque", scanned=""
    )
    assert result.text is not None
    words = shlex.split(result.text.replace(" \\\n  ", " "))
    assert words[words.index("--", 1) + 1 :] == [
        "./a/node_modules" if action is Action.ARCHIVE else "/media/disque/a/node_modules"
    ]
    assert [t.rel_path_display for t, _ in result.refused] == [""]
    assert "disque" in result.refused[0][1]


def test_only_refused_targets_give_no_command() -> None:
    result = cleanup_command(Action.DELETE, [target("")], "/home/moi", scanned="")
    assert result.text is None and len(result.refused) == 1


def test_a_hostile_name_stays_one_argument() -> None:
    hostile = "x; sudo rm -rf / #"
    result = cleanup_command(Action.DELETE, [target(hostile)], "/mnt/Data", scanned="")
    assert result.text is not None
    words = shlex.split(result.text.replace(" \\\n  ", " "))
    assert words == ["rm", "-rf", "--", f"/mnt/Data/{hostile}"]


@pytest.mark.parametrize(
    "text",
    [
        "sudo rm -rf -- \\\n  /mnt/Data/x",
        "rm -rf -- \\\n  /",
        "rm -rf -- \\\n  /mnt/Data/x \\\n  /",
        "rm -rf -- \\\n  /media/disque",
        "rm -rf --no-preserve-root -- \\\n  /mnt/Data/x",
        "rm -rf -- \\\n  /mnt/Data/x; rm -rf /",
        "rm -rf -- \\\n  /mnt/Data/x && sudo rm -rf /",
        "rm -rf --",
    ],
)
def test_the_second_check_reads_the_text_itself(text: str) -> None:
    with pytest.raises(UnsafeTarget):
        verify_command(text, Action.DELETE, "a.tar.gz", "/mnt/Data", "/mnt/Data")


def test_the_second_check_accepts_what_the_first_built() -> None:
    result = cleanup_command(
        Action.ARCHIVE, [target("a"), target("b c")], "/mnt/Data", scanned="", archive_name="x.tgz"
    )
    assert result.text is not None
    assert verify_command(result.text, Action.ARCHIVE, "x.tgz", "/mnt/Data", "/mnt/Data") == 2


@pytest.mark.parametrize(
    "command", ["sudo npm ci", "npm ci && rm -rf /", "/usr/bin/sudo make", "find . -delete"]
)
def test_recreate_commands_never_privileged_nor_destructive(command: str) -> None:
    with pytest.raises(RuleError, match="forbidden"):
        _check_template(command, "rule test")
