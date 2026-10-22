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
- create any socket other than TCP (no UDP, no Unix socket such as Docker's)
  nor use io_uring: a seccomp filter, since Landlock does not cover those;
- change permissions, owner, dates or extended attributes of any file
  (chmod, chown, utime, setxattr...): the same seccomp filter, since Landlock
  does not cover metadata either (found by this study's tests).

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

# seccomp, per machine: audit architecture, socket() number, and the system
# calls that change metadata (permissions, owner, dates, extended attributes).
_ARCHES: dict[str, tuple[int, int, tuple[int, ...]]] = {
    "x86_64": (
        0xC000003E,
        41,
        # chmod fchmod chown fchown lchown utime utimes futimesat fchownat fchmodat
        # utimensat setxattr lsetxattr fsetxattr removexattr lremovexattr
        # fremovexattr fchmodat2 setxattrat removexattrat
        (
            *(90, 91, 92, 93, 94, 132, 235, 261, 260, 268, 280),
            *(188, 189, 190, 197, 198, 199, 452, 463, 466),
        ),
    ),
    "aarch64": (
        0xC00000B7,
        198,
        # fchmod fchmodat fchownat fchown utimensat setxattr lsetxattr fsetxattr
        # removexattr lremovexattr fremovexattr fchmodat2 setxattrat removexattrat
        (52, 53, 54, 55, 88, 5, 6, 7, 14, 15, 16, 452, 463, 466),
    ),
}
_X32_SYSCALL_BIT = 0x40000000
_AF_INET, _AF_INET6, _SOCK_STREAM, _SOCK_TYPE_MASK = 2, 10, 1, 0xF
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_LD_W_ABS, _JEQ_K, _JGE_K, _AND_K, _RET_K = 0x20, 0x15, 0x35, 0x54, 0x06


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


def _seccomp_filter(
    arch: int, socket_nr: int, metadata: tuple[int, ...]
) -> list[tuple[int, int, int, int]]:
    """BPF program: only TCP sockets (IPv4, IPv6) may be created; no io_uring;
    no change of permissions, owner, dates or extended attributes.

    Denied calls fail with EACCES (the process goes on, the change is not made).
    Jump offsets count from the next instruction. seccomp_data: nr at 0, arch
    at 4, args[i] at 16 + 8 * i (low 32 bits first, little-endian).
    """
    deny = _RET_ERRNO | errno.EACCES
    head = [
        (_LD_W_ABS, 0, 0, 4),  # arch
        (_JEQ_K, 1, 0, arch),  # known arch: skip the next instruction
        (_RET_K, 0, 0, _RET_KILL_PROCESS),  # (never: installed for this arch only)
        (_LD_W_ABS, 0, 0, 0),  # syscall number
        (_JGE_K, 0, 1, _X32_SYSCALL_BIT),  # x32 ABI: other numbers for the same calls
        (_RET_K, 0, 0, deny),
    ]
    # One test per denied number: on a match, jump to the shared "deny" at the end.
    denied = (_SYS_IO_URING_SETUP, *metadata)  # io_uring could create sockets itself
    socket_part = [
        (_JEQ_K, 1, 0, socket_nr),  # socket(): check its arguments
        (_RET_K, 0, 0, _RET_ALLOW),
        (_LD_W_ABS, 0, 0, 16),  # family
        (_JEQ_K, 1, 0, _AF_INET),
        (_JEQ_K, 0, 4, _AF_INET6),  # neither: deny
        (_LD_W_ABS, 0, 0, 24),  # type
        (_AND_K, 0, 0, _SOCK_TYPE_MASK),  # without SOCK_NONBLOCK / SOCK_CLOEXEC
        (_JEQ_K, 0, 1, _SOCK_STREAM),
        (_RET_K, 0, 0, _RET_ALLOW),
        (_RET_K, 0, 0, deny),  # shared "deny"
    ]
    checks = [
        (_JEQ_K, len(denied) - 1 - index + len(socket_part) - 1, 0, number)
        for index, number in enumerate(denied)
    ]
    return head + checks + socket_part


def _install_socket_filter(libc: ctypes.CDLL) -> bool:
    known = _ARCHES.get(platform.machine())
    if known is None:
        return False
    program = _seccomp_filter(*known)
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
    if require_network and platform.machine() not in _ARCHES:
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
        sockets = _install_socket_filter(libc)
        _check(
            libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)),
            "landlock_restrict_self",
        )
    finally:
        os.close(ruleset)
    return Report(abi=abi, network=network, scoped=scoped, sockets=sockets)


def _distinct[T](values: Sequence[T]) -> list[T]:
    return list(dict.fromkeys(values))
