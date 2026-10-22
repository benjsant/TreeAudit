"""Run in a child process: restrict, then try what a compromised TreeAudit would."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import socket
import sqlite3
import sys
from collections.abc import Callable

from treeaudit.core.sandbox import Policy, restrict

allowed, outside, listen_port, server_port = sys.argv[1:5]
report = restrict(Policy(write_dirs=(allowed,), listen_ports=(int(listen_port),)))
results: dict[str, str] = {}


def attempt(name: str, action: Callable[[], object]) -> None:
    try:
        action()
    except OSError as exc:
        results[name] = errno.errorcode.get(exc.errno or 0, str(exc.errno))
    except sqlite3.Error as exc:
        results[name] = f"sqlite: {exc}"
    else:
        results[name] = "ok"


def write(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x")


def read(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        handle.read()


def sqlite_work() -> None:
    conn = sqlite3.connect(os.path.join(allowed, "index.sqlite3"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    conn.commit()
    conn.close()


def listen(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", port))
        server.listen()


def connect(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


attempt("write_inside", lambda: write(os.path.join(allowed, "new.txt")))
attempt(
    "link_inside", lambda: os.link(os.path.join(allowed, "new.txt"), os.path.join(allowed, "l"))
)
attempt("unlink_inside", lambda: os.unlink(os.path.join(allowed, "l")))
attempt("sqlite_inside", sqlite_work)
attempt("read_outside", lambda: read(os.path.join(outside, "existing.txt")))
attempt("write_outside", lambda: write(os.path.join(outside, "new.txt")))
attempt("modify_outside", lambda: write(os.path.join(outside, "existing.txt")))
attempt("delete_outside", lambda: os.unlink(os.path.join(outside, "existing.txt")))
attempt("mkdir_outside", lambda: os.mkdir(os.path.join(outside, "dir")))
attempt(
    "rename_outside",
    lambda: os.rename(os.path.join(outside, "existing.txt"), os.path.join(outside, "moved.txt")),
)
attempt(
    "move_out_of_allowed",
    lambda: os.rename(os.path.join(allowed, "new.txt"), os.path.join(outside, "stolen.txt")),
)
attempt("chmod_outside", lambda: os.chmod(os.path.join(outside, "existing.txt"), 0o777))  # noqa: S103
attempt("utime_outside", lambda: os.utime(os.path.join(outside, "existing.txt"), (0, 0)))
attempt(
    "xattr_outside",
    lambda: os.setxattr(os.path.join(outside, "existing.txt"), "user.treeaudit", b"x"),
)
attempt("chown_outside", lambda: os.chown(os.path.join(outside, "existing.txt"), -1, os.getgid()))
attempt("listen_own_port", lambda: listen(int(listen_port)))
attempt("listen_other_port", lambda: listen(int(listen_port) + 1))
attempt("connect_tcp", lambda: connect(int(server_port)))
attempt("udp_socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close())
attempt("unix_socket", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
attempt("netlink_socket", lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW).close())
attempt("socketpair", lambda: [s.close() for s in socket.socketpair()])
attempt("signal_parent", lambda: os.kill(os.getppid(), 0))


# --- security review (D-038, step 4): ways around the rules above ----------------------


def fast_open(port: int) -> None:
    """TCP Fast Open connects through sendto(), not connect()."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.sendto(b"x", socket.MSG_FASTOPEN, ("127.0.0.1", port))


def connect_v6(port: int) -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(("::1", port))


def stream_protocol(protocol: int) -> None:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM, protocol).close()


def link_in(source: str, name: str) -> None:
    os.link(source, os.path.join(allowed, name))


def write_through_symlink() -> None:
    link = os.path.join(allowed, "to-outside")
    os.symlink(os.path.join(outside, "existing.txt"), link)
    write(link)


def chattr_read_only_fd() -> None:
    import fcntl
    import struct

    fd = os.open(os.path.join(outside, "existing.txt"), os.O_RDONLY)
    try:
        flags = struct.unpack("i", fcntl.ioctl(fd, 0x80086601, b"\0" * 4))[0]  # GETFLAGS
        fcntl.ioctl(fd, 0x40086602, struct.pack("i", flags | 0x80))  # SETFLAGS +noatime
    finally:
        os.close(fd)


attempt("tcp_fast_open", lambda: fast_open(int(server_port)))
attempt("connect_tcp_ipv6", lambda: connect_v6(int(server_port)))
attempt("sctp_stream_socket", lambda: stream_protocol(132))
attempt("mptcp_stream_socket", lambda: stream_protocol(262))
attempt("tcp_explicit_protocol", lambda: stream_protocol(6))
attempt("hard_link_outside_in", lambda: link_in(os.path.join(outside, "existing.txt"), "hard"))
attempt("write_through_symlink", write_through_symlink)
attempt("truncate_outside", lambda: os.truncate(os.path.join(outside, "existing.txt"), 0))
attempt("chattr_outside", chattr_read_only_fd)
# Last: on success the process would be replaced and print nothing.
print(json.dumps({"report": dataclasses.asdict(report), "results": results}), flush=True)
attempt("exec", lambda: os.execv("/bin/true", ["true"]))  # noqa: S606 - must be refused
print(json.dumps({"exec": results["exec"]}), flush=True)


def exec_from_memory() -> None:
    """A program written to memory, then run: no file in any folder."""
    with open("/bin/true", "rb") as program:
        code = program.read()
    fd = os.memfd_create("payload", 0)
    os.write(fd, code)
    os.execv(f"/proc/self/fd/{fd}", ["payload"])  # noqa: S606 - must be refused


attempt("exec_from_memory", exec_from_memory)
print(json.dumps({"exec_from_memory": results["exec_from_memory"]}), flush=True)
