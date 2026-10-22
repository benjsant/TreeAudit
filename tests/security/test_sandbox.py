"""Kernel sandbox of the native mode (study D-038): proven in a child process."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from treeaudit.core import sandbox

SCRIPTS = Path(__file__).parent / "sandbox_scripts"

needs_landlock = pytest.mark.skipif(
    sandbox.abi_version() < 6, reason="Landlock ABI 6 or later needed (Linux 6.12+)"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_script(name: str, *args: str) -> list[dict[str, Any]]:
    result = subprocess.run(  # noqa: S603 - our own script, with the test interpreter
        [sys.executable, str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


@needs_landlock
def test_a_sandboxed_process_can_neither_write_elsewhere_nor_talk_to_anyone(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "data"
    outside = tmp_path / "audited"
    allowed.mkdir()
    outside.mkdir()
    (outside / "existing.txt").write_text("original", encoding="utf-8")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        lines = run_script(
            "probe.py",
            str(allowed),
            str(outside),
            str(free_port()),
            str(server.getsockname()[1]),
        )
    report, results = lines[0]["report"], lines[0]["results"]
    assert report["network"] and report["scoped"] and report["sockets"]
    allowed_actions = {
        "write_inside",
        "link_inside",
        "unlink_inside",
        "sqlite_inside",
        "read_outside",
        "listen_own_port",
        "socketpair",  # asyncio's own wake-up pipe
        "tcp_explicit_protocol",
    }
    for name, outcome in results.items():
        expected = "ok" if name in allowed_actions else "not ok"
        assert (outcome == "ok") == (expected == "ok"), (name, outcome)
    # Untouched, whatever the attempts.
    assert (outside / "existing.txt").read_text(encoding="utf-8") == "original"
    assert sorted(os.listdir(outside)) == ["existing.txt"]
    assert lines[1] == {"exec": "EACCES"}
    assert lines[2] == {"exec_from_memory": "EACCES"}
    # The security review's findings (step 4): refused with these errors.
    assert results["chattr_outside"] == "ENOTTY"
    assert results["sctp_stream_socket"] == results["mptcp_stream_socket"] == "EACCES"


@needs_landlock
def test_treeaudit_still_scans_finds_duplicates_and_archives_when_sandboxed(
    tmp_path: Path,
) -> None:
    lines = run_script("workload.py", str(tmp_path))
    assert lines == [{"scan": "complete", "dupes": "complete", "archive": "complete"}]


def test_policy_without_landlock_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "abi_version", lambda: 0)
    with pytest.raises(sandbox.SandboxError, match="Landlock"):
        sandbox.restrict(sandbox.Policy(write_dirs=()))


def run_filter(program: list[tuple[int, int, int, int]], data: dict[int, int]) -> int:
    """A tiny classic-BPF interpreter for the instructions the filter uses."""
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x20:  # load word
            acc = data[k]
        elif code == 0x54:  # and
            acc &= k
        elif code == 0x06:  # return
            return k
        elif code in (0x15, 0x35):  # jump if equal / greater or equal
            taken = acc == k if code == 0x15 else acc >= k
            pc += jt if taken else jf
        pc += 1


@pytest.mark.parametrize("name", sorted(sandbox._MACHINES))
def test_seccomp_filter_decisions(name: str) -> None:
    machine = sandbox._MACHINES[name]
    program = sandbox._seccomp_filter(machine)
    allow, deny, notty = 0x7FFF0000, 0x00050000 | 13, 0x00050000 | 25

    def decide(nr: int, a0: int = 0, a1: int = 0, a2: int = 0) -> int:
        return run_filter(program, {0: nr, 4: machine.audit_arch, 16: a0, 24: a1, 32: a2})

    sock, ioctl = machine.socket, machine.ioctl
    assert decide(sock, 2, 1) == allow  # TCP over IPv4
    assert decide(sock, 2, 1, 6) == allow  # ... with IPPROTO_TCP spelled out
    assert decide(sock, 10, 1 | 0o2000000) == allow  # TCP over IPv6, SOCK_CLOEXEC
    assert decide(sock, 2, 1, 132) == deny  # SCTP stream
    assert decide(sock, 2, 1, 262) == deny  # MPTCP
    assert decide(sock, 2, 2) == deny  # UDP
    assert decide(sock, 1, 1) == deny  # Unix socket
    assert decide(sock, 16, 3) == deny  # netlink
    assert decide(ioctl, 3, 0x5401) == allow  # isatty
    assert decide(ioctl, 3, 0x5421) == allow  # non-blocking socket
    assert decide(ioctl, 3, 0x40086602) == notty  # chattr (FS_IOC_SETFLAGS)
    assert decide(ioctl, 3, 0x40806685) == notty  # fs-verity
    for number in machine.denied:
        assert decide(number) == deny, number
    assert decide(0x40000000 + 41) == deny  # x32 ABI
    assert decide(0) == allow and decide(1) == allow  # ordinary calls
    assert run_filter(program, {4: 0x12345678, 0: 0}) == 0x80000000  # unknown arch


def test_assembler_resolves_labels_and_refuses_far_jumps() -> None:
    program = sandbox._assemble(
        [(0x15, "b", None, 1), "a", (0x06, None, None, 0), "b", (0x06, None, None, 1)]
    )
    assert program == [(0x15, 1, 0, 1), (0x06, 0, 0, 0), (0x06, 0, 0, 1)]
    with pytest.raises(ValueError, match="out of range"):
        sandbox._assemble([(0x15, "far", None, 0), *[(0x06, None, None, 0)] * 300, "far"])


@needs_landlock
def test_the_web_server_runs_sandboxed_and_scans(tmp_path: Path) -> None:
    port = free_port()
    with subprocess.Popen(  # noqa: S603 - our own script, with the test interpreter
        [sys.executable, str(SCRIPTS / "serve.py"), str(tmp_path), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as server:
        try:
            state = _scan_through_the_api(server, f"http://127.0.0.1:{port}")
        finally:
            server.terminate()
            server.wait(timeout=10)
    assert state["status"] == "complete", state


def _scan_through_the_api(server: subprocess.Popen[bytes], base: str) -> dict[str, Any]:
    import time
    import urllib.request

    for _ in range(100):
        try:
            with urllib.request.urlopen(f"{base}/", timeout=1) as page:  # noqa: S310
                assert page.status == 200
                break
        except OSError:
            if server.poll() is not None:
                pytest.fail(server.stderr.read().decode() if server.stderr else "exited")
            time.sleep(0.1)
    request = urllib.request.Request(  # noqa: S310 - local test server
        f"{base}/api/scans",
        data=json.dumps({"root_id": "disk", "subpath": ""}).encode(),
        headers={"Content-Type": "application/json", "X-TreeAudit-Request": "1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as started:  # noqa: S310
        assert started.status == 202
    state: dict[str, Any] = {}
    for _ in range(100):
        with urllib.request.urlopen(f"{base}/api/jobs/current", timeout=5) as job:  # noqa: S310
            state = json.loads(job.read())["job"]
        if not state["running"]:
            break
        time.sleep(0.1)
    return state


@needs_landlock
def test_the_command_line_enters_the_sandbox_natively(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"hello")
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path / "data"}"\n'
        f'[[roots]]\nid = "r"\npath = "{root}"\nallow_system_volume = true\n',
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    (tmp_path / "run").mkdir(mode=0o700)
    result = subprocess.run(  # noqa: S603 - our own package, with the test interpreter
        [sys.executable, "-P", "-m", "treeaudit", "--config", str(config), "scan", "run", "r"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "kernel sandbox on" in result.stderr
    assert "scan 1 : complet" in result.stdout
    # SQLite's temporary folder is TreeAudit's own, inside the runtime folder.
    assert (tmp_path / "run" / "treeaudit" / "tmp").is_dir()


def test_the_network_requirement_is_checked_before_restricting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "abi_version", lambda: 3)
    with pytest.raises(sandbox.SandboxError, match="Docker"):
        sandbox.restrict(sandbox.Policy(write_dirs=()), require_network=True)


def test_the_sandbox_refuses_to_start_with_other_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    monkeypatch.setattr(threading, "active_count", lambda: 2)
    with pytest.raises(sandbox.SandboxError, match="fils"):
        sandbox.restrict(sandbox.Policy(write_dirs=()))


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("/home/me/.local/share/treeaudit/index", True),
        ("/mnt/Data/treeaudit/archives", True),
        ("/home/me", False),  # the whole personal folder
        ("/home", False),
        ("/", False),
        ("/etc", False),
        ("/usr/local", False),
        ("/etc/treeaudit", False),
        ("/var/home/me/.local/share/treeaudit/index", True),  # Fedora Silverblue
        ("/run/user/1000/treeaudit/tmp", True),  # SQLite's temporary folder
        ("relative/data", False),
    ],
)
def test_write_folders_must_be_treeaudits_own(path: str, accepted: bool) -> None:
    from treeaudit import cli

    if accepted:
        cli._check_write_dir(path, "/home/me")
    else:
        with pytest.raises(sandbox.SandboxError, match="refusé"):
            cli._check_write_dir(path, "/home/me")


def test_the_gateway_is_refused_natively(capsys: pytest.CaptureFixture[str]) -> None:
    from treeaudit.cli import EXIT_ERROR, main

    assert main(["gateway", "--upstream-host", "app"], sandboxed=True) == EXIT_ERROR
    assert "Docker" in capsys.readouterr().err
