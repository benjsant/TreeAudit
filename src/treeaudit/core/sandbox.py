"""Kernel sandbox for the native mode: Landlock and seccomp (study, D-038).

Without Docker, the kernel itself must keep TreeAudit read-only. Once
``restrict()`` has run, the process (and anything it could start) can never:

- write, create, rename or delete anything outside the given folders
  (TreeAudit's data and archive folders): Landlock, filesystem rights;
- execute a program: Landlock, ``EXECUTE`` is never granted;
- open an outgoing TCP connection, nor listen on another port than its own:
  Landlock, network rights (ABI 4);
- signal other processes or use their abstract Unix sockets: Landlock scopes
  (ABI 6);
- create any socket other than TCP (no UDP, no Unix socket such as Docker's,
  no SCTP nor MPTCP stream) nor use io_uring: a seccomp filter, since
  Landlock does not cover those;
- change permissions, owner, dates, extended attributes or flags of any file
  (chmod, chown, utime, setxattr, and ioctl(): only a few terminal and
  non-blocking requests are allowed, chattr is not): the same filter, since
  Landlock does not cover metadata either (found by the study and the
  security review);
- execute a program, or reach other processes (ptrace, their memory or
  descriptors): refused by Landlock, and again by the filter.

Reading stays allowed everywhere the user may read: the folder to audit is
chosen in the interface. These restrictions cannot be lifted by the process.

Only two C functions are called, ``syscall`` and ``prctl``, through ctypes:
the standard library has no binding for them. This is the only module allowed
to import ctypes (tests/security/test_static_code.py).
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from treeaudit.core.errors import TreeAuditError

# Landlock system calls: the same numbers on every architecture (added after
# the unification of the syscall tables).
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_SYS_IO_URING_SETUP = 425
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_RULE_NET_PORT = 2

# Filesystem rights, by the ABI version that introduced them.
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_V1 = (1 << 13) - 1  # execute ... make_sym
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


@dataclass(frozen=True, slots=True)
class _Machine:
    """System call numbers of one architecture, for the seccomp filter."""

    audit_arch: int
    socket: int
    ioctl: int
    denied: tuple[int, ...]
    """Always refused: metadata changes (Landlock does not cover them), program
    execution, other processes' memory and descriptors (Landlock already
    refuses these: defense in depth), and io_uring (it can create sockets)."""


_MACHINES: dict[str, _Machine] = {
    "x86_64": _Machine(
        audit_arch=0xC000003E,
        socket=41,
        ioctl=16,
        denied=(
            # chmod fchmod chown fchown lchown utime utimes futimesat fchownat fchmodat
            # utimensat setxattr lsetxattr fsetxattr removexattr lremovexattr
            # fremovexattr fchmodat2 setxattrat removexattrat
            *(90, 91, 92, 93, 94, 132, 235, 261, 260, 268, 280),
            *(188, 189, 190, 197, 198, 199, 452, 463, 466),
            # execve execveat ptrace process_vm_readv process_vm_writev pidfd_getfd
            # io_uring_setup
            *(59, 322, 101, 310, 311, 438, 425),
        ),
    ),
    "aarch64": _Machine(
        audit_arch=0xC00000B7,
        socket=198,
        ioctl=29,
        denied=(
            # fchmod fchmodat fchownat fchown utimensat setxattr lsetxattr fsetxattr
            # removexattr lremovexattr fremovexattr fchmodat2 setxattrat removexattrat
            *(52, 53, 54, 55, 88, 5, 6, 7, 14, 15, 16, 452, 463, 466),
            # execve execveat ptrace process_vm_readv process_vm_writev pidfd_getfd
            # io_uring_setup
            *(221, 281, 117, 270, 271, 438, 425),
        ),
    ),
}
# The only ioctl() requests allowed (the same numbers on both architectures):
# terminal queries and non-blocking mode, which Python itself uses. Everything
# else is refused, since many requests change files even through a read-only
# descriptor (chattr's FS_IOC_SETFLAGS, fs-verity, Btrfs...): found by the
# security review of step 4.
_ALLOWED_IOCTLS = (
    0x5401,  # TCGETS (isatty)
    0x540F,  # TIOCGPGRP
    0x5413,  # TIOCGWINSZ
    0x541B,  # FIONREAD
    0x5421,  # FIONBIO (socket.setblocking)
    0x5450,  # FIONCLEX
    0x5451,  # FIOCLEX
)
_X32_SYSCALL_BIT = 0x40000000
_AF_INET, _AF_INET6, _SOCK_STREAM, _SOCK_TYPE_MASK = 2, 10, 1, 0xF
_IPPROTO_TCP = 6
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_LD_W_ABS, _JEQ_K, _JGE_K, _AND_K, _RET_K = 0x20, 0x15, 0x35, 0x54, 0x06

# A BPF instruction before assembly: jump targets are labels (None: the next
# instruction). A bare string is a label.
type _Op = tuple[int, str | None, str | None, int] | str


def _assemble(ops: Sequence[_Op]) -> list[tuple[int, int, int, int]]:
    """Resolve labels into the relative jump offsets classic BPF uses."""
    labels: dict[str, int] = {}
    position = 0
    for op in ops:
        if isinstance(op, str):
            labels[op] = position
        else:
            position += 1
    program: list[tuple[int, int, int, int]] = []
    for op in ops:
        if isinstance(op, str):
            continue
        code, true, false, k = op
        here = len(program)
        offsets = [0 if target is None else labels[target] - here - 1 for target in (true, false)]
        if not all(0 <= offset <= 255 for offset in offsets):
            raise ValueError(f"jump out of range at instruction {here}")
        program.append((code, offsets[0], offsets[1], k))
    return program


class SandboxError(TreeAuditError):
    """The kernel sandbox could not be set up: TreeAudit must not start natively."""


@dataclass(frozen=True, slots=True)
class Policy:
    write_dirs: tuple[str, ...]
    """The only folders where anything may be written (data, archives)."""
    listen_ports: tuple[int, ...] = ()
    """TCP ports the server may listen on. Outgoing connections are never allowed."""


@dataclass(frozen=True, slots=True)
class Report:
    abi: int
    network: bool
    """Outgoing TCP blocked by Landlock (ABI 4 or later)."""
    scoped: bool
    """Signals and abstract Unix sockets scoped (ABI 6 or later)."""
    sockets: bool
    """Non-TCP sockets, io_uring and metadata changes blocked by seccomp."""

    @property
    def complete(self) -> bool:
        return self.network and self.scoped and self.sockets


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    """Landlock ABI version of the running kernel; 0 if Landlock is unavailable."""
    libc = _libc()
    result = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(int(result), 0)


def _fs_rights(abi: int) -> int:
    rights = _FS_V1
    if abi >= 2:
        rights |= _FS_REFER
    if abi >= 3:
        rights |= _FS_TRUNCATE
    if abi >= 5:
        rights |= _FS_IOCTL_DEV
    return rights


def _check(result: int, what: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise SandboxError(f"{what}: {os.strerror(code)} (errno {code})")
    return result


def _add_path_rule(libc: ctypes.CDLL, ruleset: int, path: str, rights: int) -> None:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        # struct landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; } (packed)
        attr = ctypes.create_string_buffer(struct.pack("=Qi", rights, fd))
        _check(
            libc.syscall(
                _SYS_LANDLOCK_ADD_RULE,
                ctypes.c_int(ruleset),
                ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                attr,
                ctypes.c_uint32(0),
            ),
            f"landlock_add_rule({path})",
        )
    finally:
        os.close(fd)


def _add_port_rule(libc: ctypes.CDLL, ruleset: int, port: int) -> None:
    # struct landlock_net_port_attr { __u64 allowed_access; __u64 port; }
    attr = ctypes.create_string_buffer(struct.pack("=QQ", _NET_BIND_TCP, port))
    _check(
        libc.syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset),
            ctypes.c_int(_LANDLOCK_RULE_NET_PORT),
            attr,
            ctypes.c_uint32(0),
        ),
        f"landlock_add_rule(port {port})",
    )


def _seccomp_filter(machine: _Machine) -> list[tuple[int, int, int, int]]:
    """BPF program: only TCP sockets; only a few harmless ioctl() requests; no
    metadata change, execution, access to other processes, nor io_uring.

    Refused calls fail with an error (EACCES, ENOTTY for ioctl): the process goes
    on, nothing is changed. seccomp_data: nr at 0, arch at 4, args[i] at
    16 + 8 * i (low 32 bits first, little-endian).
    """
    return _assemble(
        [
            (_LD_W_ABS, None, None, 4),
            (_JEQ_K, "known_arch", None, machine.audit_arch),
            (_RET_K, None, None, _RET_KILL_PROCESS),  # never: installed for this arch only
            "known_arch",
            (_LD_W_ABS, None, None, 0),
            (_JGE_K, "deny", None, _X32_SYSCALL_BIT),  # x32: other numbers, same calls
            (_JEQ_K, "socket", None, machine.socket),
            (_JEQ_K, "ioctl", None, machine.ioctl),
            *[(_JEQ_K, "deny", None, number) for number in machine.denied],
            (_RET_K, None, None, _RET_ALLOW),
            "socket",  # family, type and protocol: TCP over IPv4 or IPv6 only
            (_LD_W_ABS, None, None, 16),
            (_JEQ_K, "type", None, _AF_INET),
            (_JEQ_K, "type", "deny", _AF_INET6),
            "type",
            (_LD_W_ABS, None, None, 24),
            (_AND_K, None, None, _SOCK_TYPE_MASK),  # without SOCK_NONBLOCK / SOCK_CLOEXEC
            (_JEQ_K, "protocol", "deny", _SOCK_STREAM),
            "protocol",  # a stream socket may also be SCTP or MPTCP: not TCP
            (_LD_W_ABS, None, None, 32),
            (_JEQ_K, "allow", None, 0),
            (_JEQ_K, "allow", "deny", _IPPROTO_TCP),
            "ioctl",  # the request is an unsigned int for the kernel: 32 bits suffice
            (_LD_W_ABS, None, None, 24),
            *[(_JEQ_K, "allow", None, request) for request in _ALLOWED_IOCTLS],
            (_RET_K, None, None, _RET_ERRNO | errno.ENOTTY),
            "allow",
            (_RET_K, None, None, _RET_ALLOW),
            "deny",
            (_RET_K, None, None, _RET_ERRNO | errno.EACCES),
        ]
    )


def _install_seccomp_filter(libc: ctypes.CDLL) -> bool:
    machine = _MACHINES.get(platform.machine())
    if machine is None:
        return False
    program = _seccomp_filter(machine)
    filters = ctypes.create_string_buffer(
        b"".join(struct.pack("=HBBI", *instruction) for instruction in program)
    )
    # struct sock_fprog { unsigned short len; struct sock_filter *filter; }
    fprog = ctypes.create_string_buffer(
        struct.pack("@HP", len(program), ctypes.addressof(filters))  # native alignment
    )
    _check(
        libc.prctl(
            ctypes.c_int(_PR_SET_SECCOMP),
            ctypes.c_ulong(_SECCOMP_MODE_FILTER),
            fprog,
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        ),
        "seccomp",
    )
    return True


def restrict(policy: Policy, *, require_network: bool = False) -> Report:
    """Restrict this process for good. Raises SandboxError, before restricting
    anything, if Landlock is missing or, with ``require_network``, if outgoing
    connections and non-TCP sockets cannot both be blocked.

    Landlock and seccomp apply to the calling thread and to the threads it
    starts later: called while other threads run, they would escape it.
    """
    abi = abi_version()
    if abi < 1:
        raise SandboxError("Landlock n'est pas disponible dans ce noyau (Linux 5.13 ou plus)")
    if require_network and abi < 4:
        raise SandboxError(
            f"ce noyau ne permet pas de couper le réseau (Landlock ABI {abi}, il faut 4, "
            "Linux 6.7 ou plus) : utilise TreeAudit avec Docker"
        )
    if require_network and platform.machine() not in _MACHINES:
        raise SandboxError(f"architecture {platform.machine()} non prise en charge par le filtre")
    if threading.active_count() != 1:
        raise SandboxError("le bac à sable doit être posé avant de lancer d'autres fils")
    libc = _libc()
    fs_all = _fs_rights(abi)
    network = abi >= 4
    scoped = abi >= 6
    # struct landlock_ruleset_attr { __u64 handled_access_fs, handled_access_net, scoped; }
    fields = [fs_all]
    if network:
        fields.append(_NET_BIND_TCP | _NET_CONNECT_TCP)
    if scoped:
        fields.append(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)
    attr = ctypes.create_string_buffer(struct.pack(f"={len(fields)}Q", *fields))
    ruleset = _check(
        libc.syscall(
            _SYS_LANDLOCK_CREATE_RULESET, attr, ctypes.c_size_t(len(attr.raw)), ctypes.c_uint32(0)
        ),
        "landlock_create_ruleset",
    )
    try:
        _add_path_rule(libc, ruleset, "/", _FS_READ_FILE | _FS_READ_DIR)
        for folder in _distinct(policy.write_dirs):
            _add_path_rule(libc, ruleset, folder, fs_all & ~_FS_EXECUTE)
        if network:
            for port in _distinct(policy.listen_ports):
                _add_port_rule(libc, ruleset, port)
        _check(
            libc.prctl(
                ctypes.c_int(_PR_SET_NO_NEW_PRIVS),
                ctypes.c_ulong(1),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
            ),
            "no_new_privs",
        )
        sockets = _install_seccomp_filter(libc)
        _check(
            libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)),
            "landlock_restrict_self",
        )
    finally:
        os.close(ruleset)
    return Report(abi=abi, network=network, scoped=scoped, sockets=sockets)


def _distinct[T](values: Sequence[T]) -> list[T]:
    return list(dict.fromkeys(values))
