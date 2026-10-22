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
    }
    for name, outcome in results.items():
        expected = "ok" if name in allowed_actions else "not ok"
        assert (outcome == "ok") == (expected == "ok"), (name, outcome)
    # Untouched, whatever the attempts.
    assert (outside / "existing.txt").read_text(encoding="utf-8") == "original"
    assert sorted(os.listdir(outside)) == ["existing.txt"]
    assert lines[1] == {"exec": "EACCES"}


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


@pytest.mark.parametrize("machine", sorted(sandbox._ARCHES))
def test_seccomp_filter_decisions(machine: str) -> None:
    arch, socket_nr, metadata = sandbox._ARCHES[machine]
    program = sandbox._seccomp_filter(arch, socket_nr, metadata)
    allow, deny = 0x7FFF0000, 0x00050000 | 13

    def decide(nr: int, family: int = 0, kind: int = 0) -> int:
        return run_filter(program, {0: nr, 4: arch, 16: family, 24: kind})

    assert decide(socket_nr, 2, 1) == allow  # TCP over IPv4
    assert decide(socket_nr, 10, 1 | 0o2000000) == allow  # TCP over IPv6, SOCK_CLOEXEC
    assert decide(socket_nr, 2, 2) == deny  # UDP
    assert decide(socket_nr, 1, 1) == deny  # Unix socket
    assert decide(socket_nr, 16, 3) == deny  # netlink
    assert decide(425) == deny  # io_uring
    for number in metadata:
        assert decide(number) == deny, number
    assert decide(0x40000000 + 41) == deny  # x32 ABI
    assert decide(0) == allow and decide(1) == allow  # read, write...
    assert run_filter(program, {4: 0x12345678, 0: 0}) == 0x80000000  # unknown arch


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
