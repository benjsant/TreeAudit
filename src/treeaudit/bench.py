"""Measurements for `treeaudit bench`: numbers only, never a file name.

Reports wall and CPU time, peak memory and what the process really asked the
storage layer for (/proc/self/io), plus whether the kernel says the disk is
rotational. Meant to be run by the user on their own disks; the output can be
shared without revealing anything about their files.
"""

from __future__ import annotations

import os
import resource
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_PROC_IO = Path("/proc/self/io")


def _io_counters() -> dict[str, int]:
    """read_bytes (from storage), rchar (all reads, cache included), syscr (read calls)."""
    try:
        text = _PROC_IO.read_text("ascii")
    except OSError:
        return {}
    counters = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if value.strip().isdigit():
            counters[key.strip()] = int(value)
    return counters


@dataclass(frozen=True, slots=True)
class Measure:
    wall: float
    cpu: float
    peak_rss_mib: int
    """Peak resident memory of the whole process so far (MiB)."""
    storage_read: int | None
    """Bytes actually fetched from the storage layer (not served by the page cache)."""
    read_calls: int | None


def measure[T](work: Callable[[], T]) -> tuple[T, Measure]:
    before_io = _io_counters()
    before = resource.getrusage(resource.RUSAGE_SELF)
    start = time.monotonic()
    result = work()
    wall = time.monotonic() - start
    after = resource.getrusage(resource.RUSAGE_SELF)
    after_io = _io_counters()

    def delta(key: str) -> int | None:
        if key in before_io and key in after_io:
            return after_io[key] - before_io[key]
        return None

    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return result, Measure(
        wall=wall,
        cpu=cpu,
        peak_rss_mib=after.ru_maxrss // 1024,
        storage_read=delta("read_bytes"),
        read_calls=delta("syscr"),
    )


def disk_kind(path: str) -> str:
    """'mécanique', 'SSD' or 'inconnu', as reported by the kernel for the device of path."""
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return "inconnu"
    base = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
    for candidate in (base / "queue" / "rotational", base / ".." / "queue" / "rotational"):
        try:
            flag = candidate.read_text("ascii").strip()
        except OSError:
            continue
        return "mécanique" if flag == "1" else "SSD"
    return "inconnu"
