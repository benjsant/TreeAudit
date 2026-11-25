"""Similar images, from the signatures the page computed (D-069).

Two images are similar when their 256-bit gradient hashes differ by at most
MAX_BITS bits AND their 32 x 32 grey thumbnails correlate at MIN_CORRELATION
or more. The hash finds the candidates; the correlation keeps apart pictures
the coarse hash confuses (photos of different screens or documents). Both
thresholds were measured on test variants and real phone photos.

Finding the candidates without comparing every pair: the hash is cut into
CHUNKS pieces. Two hashes within MAX_BITS bits differ by at most
MAX_BITS // CHUNKS bits in at least one piece (pigeonhole), so each image only
looks at the images whose piece is that close to one of its own.

Similar pairs are joined into groups. The best version is kept: the most
pixels, then the largest file, then the oldest. Only images directly similar
to the kept one are proposed for removal: in a burst that drifts (A like B,
B like C), C is shown but not proposed when it is not like A. Images inside
software or a git repository are never proposed (duplicates/groups.py).
Nothing on the disk is read here, nothing is changed.
"""

from __future__ import annotations

import operator
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from treeaudit.db import cache
from treeaudit.db.images import ALGO, HASH_BYTES, THUMB_BYTES
from treeaudit.duplicates.groups import Guarded, guarded_places, protection_reason

MAX_BITS = 30
MIN_CORRELATION = 0.95
CHUNKS = 16
_CHUNK_BITS = HASH_BYTES * 8 // CHUNKS
_CHUNK_MASK = (1 << _CHUNK_BITS) - 1
_CHUNK_RADIUS = MAX_BITS // CHUNKS


@dataclass(frozen=True, slots=True)
class Image:
    """A signed image of the analysis, with what the choice of the kept one uses."""

    entry_id: int
    rel_path: bytes
    rel_path_display: str
    size: int
    mtime_ns: int
    width: int
    height: int
    hash: int
    thumb: bytes
    thumb_sum: int
    thumb_squares: int

    @property
    def pixels(self) -> int:
        """Resolution, the first criterion of the version to keep."""
        return self.width * self.height


def bits_between(a: Image, b: Image) -> int:
    """How many bits differ between the two hashes."""
    return (a.hash ^ b.hash).bit_count()


def correlation(a: Image, b: Image) -> float:
    """Pearson correlation of the two thumbnails, from integer sums: the same
    value as the prototype's (tools/similar_images_proto/analyse.py). A flat
    thumbnail has none: 0.0, never similar."""
    n = THUMB_BYTES
    cross = sum(map(operator.mul, a.thumb, b.thumb))
    covariance = n * cross - a.thumb_sum * b.thumb_sum
    spread_a = n * a.thumb_squares - a.thumb_sum * a.thumb_sum
    spread_b = n * b.thumb_squares - b.thumb_sum * b.thumb_sum
    if spread_a <= 0 or spread_b <= 0:
        return 0.0
    return float(covariance / (spread_a * spread_b) ** 0.5)


def similar(a: Image, b: Image) -> bool:
    """Both tests of the module docstring."""
    return bits_between(a, b) <= MAX_BITS and correlation(a, b) >= MIN_CORRELATION


@dataclass(frozen=True, slots=True)
class Member:
    """An image of a group, compared with the one kept."""

    image: Image
    bits: int
    """Differing hash bits with the kept image (0 for itself)."""
    correlation: float
    close: bool
    """Directly similar to the kept image: only those may be proposed."""
    protection: str | None


@dataclass(frozen=True, slots=True)
class Group:
    """Images similar to one another; the first member is the one kept."""

    members: tuple[Member, ...]
    reasons: tuple[str, ...]
    """Why the first one is kept."""

    @property
    def keep(self) -> Member:
        """The version proposed to keep."""
        return self.members[0]

    @property
    def removable(self) -> tuple[Member, ...]:
        """Members that could go: not the kept one, directly like it, not protected."""
        return tuple(m for m in self.members[1:] if m.close and m.protection is None)

    @property
    def recoverable(self) -> int:
        """Bytes freed by removing every removable member."""
        return sum(m.image.size for m in self.removable)


@dataclass(frozen=True, slots=True)
class Report:
    """The groups of an analysis, the most space to recover first; a group's
    number is its rank, from 1."""

    groups: tuple[Group, ...]
    signed: int
    """Images with a signature taken into account."""
    failed: int
    """Images the page could not sign."""
    version: str
    """Changes whenever a signature is stored: a command prepared on an older
    page is refused, its group numbers may mean other groups now."""

    @property
    def removable(self) -> int:
        """Images that could go, over every group."""
        return sum(len(g.removable) for g in self.groups)

    @property
    def recoverable(self) -> int:
        """Bytes freed by removing them all."""
        return sum(g.recoverable for g in self.groups)


class SelectionError(ValueError):
    """A choice of the page that does not fit the groups (shown in French)."""


_SIGNED_SQL = (
    "SELECT s.entry_id, s.hash, s.thumb, s.width, s.height, e.rel_path, e.rel_path_display, "
    "e.size, e.mtime_ns, e.dev, e.ino FROM image_sig s "
    "JOIN entry e ON e.scan_id = s.scan_id AND e.entry_id = s.entry_id "
    "WHERE s.scan_id = ? AND s.algo = ? AND s.failure IS NULL ORDER BY e.rel_path"
)


def load_images(conn: sqlite3.Connection, scan_id: int) -> list[Image]:
    """The signed images of an analysis. Hard links of one file count once:
    removing one name frees nothing while another remains."""
    images: list[Image] = []
    seen: set[tuple[int, int]] = set()
    for row in conn.execute(_SIGNED_SQL, (scan_id, ALGO)):
        inode = (row["dev"], row["ino"])
        if inode in seen:
            continue
        seen.add(inode)
        thumb = bytes(row["thumb"])
        images.append(
            Image(
                entry_id=row["entry_id"],
                rel_path=bytes(row["rel_path"]),
                rel_path_display=row["rel_path_display"],
                size=row["size"],
                mtime_ns=row["mtime_ns"],
                width=row["width"],
                height=row["height"],
                hash=int.from_bytes(bytes(row["hash"]), "big"),
                thumb=thumb,
                thumb_sum=sum(thumb),
                thumb_squares=sum(v * v for v in thumb),
            )
        )
    return images


def _near(value: int) -> list[int]:
    """A piece and every value one bit away (CHUNK radius 1)."""
    return [value] + [value ^ (1 << bit) for bit in range(_CHUNK_BITS)]


def similar_pairs(images: list[Image]) -> list[tuple[int, int]]:
    """Every pair (index, index) of similar images, each pair once."""
    if _CHUNK_RADIUS != 1:  # _near() enumerates radius 1 only
        raise ValueError("MAX_BITS // CHUNKS must be 1")
    tables: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(CHUNKS)]
    for index, image in enumerate(images):
        for chunk in range(CHUNKS):
            tables[chunk][(image.hash >> (chunk * _CHUNK_BITS)) & _CHUNK_MASK].append(index)
    pairs: list[tuple[int, int]] = []
    for index, image in enumerate(images):
        candidates: set[int] = set()
        for chunk in range(CHUNKS):
            table = tables[chunk]
            for value in _near((image.hash >> (chunk * _CHUNK_BITS)) & _CHUNK_MASK):
                candidates.update(other for other in table.get(value, ()) if other > index)
        pairs.extend(
            (index, other) for other in sorted(candidates) if similar(image, images[other])
        )
    return pairs


def _keep_order(image: Image) -> tuple[int, int, int, int, bytes]:
    return (-image.pixels, -image.size, image.mtime_ns, len(image.rel_path), image.rel_path)


def _reasons(kept: Image, others: list[Image]) -> tuple[str, ...]:
    if all(kept.pixels > o.pixels for o in others):
        return ("la plus grande résolution",)
    if all(kept.pixels >= o.pixels and kept.size > o.size for o in others):
        return ("à résolution égale, le fichier le plus lourd",)
    if all(kept.mtime_ns < o.mtime_ns for o in others):
        return ("la plus ancienne (probablement l'originale)",)
    return ("chemin le plus court",)


def make_group(images: list[Image], guarded: Guarded, kept: Image | None = None) -> Group:
    """One group: the best version first (or ``kept``, the user's choice),
    every member compared with it."""
    ordered = sorted(images, key=_keep_order)
    if kept is not None:
        ordered.remove(kept)
        ordered.insert(0, kept)
    kept = ordered[0]
    members = [Member(kept, 0, 1.0, True, protection_reason(kept.rel_path, guarded))]
    for image in ordered[1:]:
        bits, likeness = bits_between(kept, image), correlation(kept, image)
        close = bits <= MAX_BITS and likeness >= MIN_CORRELATION
        protection = protection_reason(image.rel_path, guarded)
        members.append(Member(image, bits, likeness, close, protection))
    return Group(tuple(members), _reasons(kept, ordered[1:]))


def override_keep(group: Group, position: int, guarded: Guarded) -> Group:
    """The same group with the member at ``position`` (1-based, in the order
    shown) kept instead: every other member is compared with it again."""
    if not 1 <= position <= len(group.members):
        raise SelectionError(f"pas d'image n° {position} dans ce groupe")
    images = [m.image for m in group.members]
    regrouped = make_group(images, guarded, kept=images[position - 1])
    return Group(regrouped.members, ("ton choix",))


def signatures_version(conn: sqlite3.Connection, scan_id: int) -> str:
    """How many signatures, and the last row written: any new one changes it."""
    count, last = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM image_sig WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    return f"{count}-{last}"


def _cached_pairs(
    conn: sqlite3.Connection, scan_id: int, version: str, images: list[Image]
) -> list[tuple[int, int]]:
    """similar_pairs(), kept in the index cache: the slow part (8 s for
    50 000 images), the same answer as long as no signature changes."""
    key = f"similar-images:{scan_id}:{version}:a{ALGO}:{MAX_BITS}:{MIN_CORRELATION}:{CHUNKS}"
    index_of = {image.entry_id: index for index, image in enumerate(images)}
    cached = cache.get(conn, key)
    if cached is not None:
        return [(index_of[a], index_of[b]) for a, b in cached["pairs"]]
    pairs = similar_pairs(images)
    ids = [[images[a].entry_id, images[b].entry_id] for a, b in pairs]
    cache.put(conn, key, {"pairs": ids})
    return pairs


def similar_images(conn: sqlite3.Connection, scan_id: int) -> Report:
    """The groups of similar images of an analysis, from its stored signatures."""
    version = signatures_version(conn, scan_id)
    images = load_images(conn, scan_id)
    parent = list(range(len(images)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in _cached_pairs(conn, scan_id, version, images):
        parent[root(a)] = root(b)
    components: dict[int, list[Image]] = defaultdict(list)
    for index, image in enumerate(images):
        components[root(index)].append(image)
    guarded = guarded_places(conn, scan_id)
    groups = [make_group(m, guarded) for m in components.values() if len(m) > 1]
    groups.sort(key=lambda g: (-g.recoverable, -len(g.members), g.keep.image.rel_path))
    failed = conn.execute(
        "SELECT COUNT(*) FROM image_sig WHERE scan_id = ? AND algo = ? AND failure IS NOT NULL",
        (scan_id, ALGO),
    ).fetchone()[0]
    return Report(tuple(groups), len(images), failed, version)
