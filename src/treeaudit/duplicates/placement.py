"""Physical placement of files, to read them in disk order (rotating disks).

On a 5400 rpm disk every seek costs about 10-15 ms, so reading candidates in
the order of their position on the platters instead of an arbitrary order can
divide the time by a large factor. Linux reports the position with the FIEMAP
ioctl: read-only, available on ext4, btrfs, xfs and ntfs3 among others. When it
is not available (tmpfs, FUSE, network filesystems), the inode number is used:
on ext4 it correlates with the position of the inode, often of the data too.
"""

from __future__ import annotations

import fcntl
import struct

# _IOWR('f', 11, struct fiemap) with sizeof(struct fiemap) == 32
FS_IOC_FIEMAP = 0xC020660B
_HEADER = struct.Struct("=QQIIII")  # fm_start, fm_length, fm_flags, fm_mapped_extents,
#                                     fm_extent_count, fm_reserved
_EXTENT = struct.Struct("=QQQQQIIII")  # fe_logical, fe_physical, fe_length, 2 reserved,
#                                        fe_flags, 3 reserved
_WHOLE_FILE = 0xFFFFFFFFFFFFFFFF


def physical_offset(fd: int) -> int | None:
    """Physical byte offset of the first extent of an open file, or None."""
    buffer = bytearray(_HEADER.size + _EXTENT.size)
    _HEADER.pack_into(buffer, 0, 0, _WHOLE_FILE, 0, 0, 1, 0)
    try:
        fcntl.ioctl(fd, FS_IOC_FIEMAP, buffer, True)
    except OSError:
        return None  # not supported by this filesystem
    if _HEADER.unpack_from(buffer, 0)[3] < 1:
        return None  # empty or not yet allocated
    physical = _EXTENT.unpack_from(buffer, _HEADER.size)[1]
    return physical if physical > 0 else None


def read_order_key(physical: int | None, dev: int, ino: int) -> tuple[int, int, int]:
    """Sort key: physical position when known, otherwise the inode number."""
    if physical is not None:
        return (0, dev, physical)
    return (1, dev, ino)
