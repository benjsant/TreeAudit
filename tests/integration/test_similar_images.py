"""Groups of similar images, from signatures stored as the page would (D-069)."""

from __future__ import annotations

import dataclasses
import itertools
import math
import os
import random
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from treeaudit.core.config import RootConfig
from treeaudit.db import images as images_db
from treeaudit.db.connection import connect_memory
from treeaudit.duplicates.groups import Guarded
from treeaudit.duplicates.runner import DupOptions, run_duplicates
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.validation import ValidationContext
from treeaudit.scanner.runner import ScanRequest, run_scan
from treeaudit.similar import images as similar

RNG = random.Random(20261125)  # noqa: S311 - reproducible test data, not a secret
LIMITS = {"extensions": {"jpg"}, "max_size": 10**6}


def random_hash() -> int:
    return RNG.getrandbits(256)


def flipped(value: int, bits: int, start: int = 0) -> int:
    """``value`` with ``bits`` distinct bits flipped, from bit ``start`` on."""
    for bit in range(start, start + bits):
        value ^= 1 << bit
    return value


def random_thumb() -> bytes:
    return bytes(RNG.randrange(256) for _ in range(images_db.THUMB_BYTES))


def nearby(thumb: bytes) -> bytes:
    """The same picture, slightly recompressed."""
    return bytes(min(255, max(0, v + RNG.randint(-3, 3))) for v in thumb)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect_memory()
    yield connection
    connection.close()


searches: dict[int, tuple[RootConfig, ValidationContext]] = {}


def search_duplicates(conn: sqlite3.Connection, scan_id: int) -> None:
    config, ctx = searches[scan_id]
    run_duplicates(conn, scan_id, config, ctx=ctx, options=DupOptions(), cancel=threading.Event())


@pytest.fixture
def analyse(
    tmp_path: Path, conn: sqlite3.Connection, data_disk_table: Callable[..., MountTable]
) -> Callable[..., tuple[int, dict[str, int]]]:
    """Scan a tree of files named .jpg; the content does not matter here."""

    def run(
        names: list[str],
        links: dict[str, str] | None = None,
        contents: dict[str, bytes] | None = None,
    ) -> tuple[int, dict[str, int]]:
        root = tmp_path / "root"
        for name in names:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((contents or {}).get(name, name.encode() * 50))
        for link, existing in (links or {}).items():
            os.link(root / existing, root / link)
        config = RootConfig(id="r", path=str(root), label="r")
        ctx = ValidationContext(mounts=data_disk_table(str(tmp_path)))
        scan_id = run_scan(
            conn, ScanRequest(root=config), ctx=ctx, cancel=threading.Event()
        ).scan_id
        searches[scan_id] = (config, ctx)
        ids = {
            row[1]: row[0]
            for row in conn.execute(
                "SELECT entry_id, rel_path_display FROM entry WHERE scan_id = ?", (scan_id,)
            )
        }
        return scan_id, ids

    return run


def sign(
    conn: sqlite3.Connection,
    scan_id: int,
    ids: dict[str, int],
    signatures: dict[str, tuple[int, bytes, int, int]],
) -> None:
    rows = [
        images_db.Signature(ids[name], h.to_bytes(32, "big"), thumb, width, height)
        for name, (h, thumb, width, height) in signatures.items()
    ]
    assert images_db.save(conn, scan_id, rows, [], **LIMITS) == []  # type: ignore[arg-type]


def paths(group: similar.Group) -> list[str]:
    return [m.image.rel_path_display for m in group.members]


def test_a_smaller_copy_is_grouped_and_the_largest_kept(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    scan_id, ids = analyse(["Vacances/plage.jpg", "Envoyées/plage-petite.jpg", "autre.jpg"])
    h, thumb = random_hash(), random_thumb()
    sign(
        conn,
        scan_id,
        ids,
        {
            "Vacances/plage.jpg": (h, thumb, 4000, 3000),
            "Envoyées/plage-petite.jpg": (flipped(h, 3), nearby(thumb), 1000, 750),
            "autre.jpg": (random_hash(), random_thumb(), 4000, 3000),
        },
    )
    report = similar.similar_images(conn, scan_id)
    assert (report.signed, report.failed) == (3, 0)
    [group] = report.groups
    assert paths(group) == ["Vacances/plage.jpg", "Envoyées/plage-petite.jpg"]
    assert group.reasons == ("la plus grande résolution",)
    assert [m.image.rel_path_display for m in group.removable] == ["Envoyées/plage-petite.jpg"]
    assert group.members[1].bits == 3 and group.members[1].correlation > 0.99


def test_photos_of_different_screens_are_not_grouped(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    """Found on real photos: close hashes, different pictures."""
    scan_id, ids = analyse(["écran1.jpg", "écran2.jpg"])
    h = random_hash()
    sign(
        conn,
        scan_id,
        ids,
        {
            "écran1.jpg": (h, random_thumb(), 4000, 3000),
            "écran2.jpg": (flipped(h, 10), random_thumb(), 4000, 3000),
        },
    )
    assert similar.similar_images(conn, scan_id).groups == ()


@pytest.mark.parametrize(("bits", "grouped"), [(30, True), (31, False)])
def test_the_hash_limit_is_exact(
    conn: sqlite3.Connection,
    analyse: Callable[..., tuple[int, dict[str, int]]],
    bits: int,
    grouped: bool,
) -> None:
    scan_id, ids = analyse(["a.jpg", "b.jpg"])
    h, thumb = random_hash(), random_thumb()
    sign(
        conn,
        scan_id,
        ids,
        {"a.jpg": (h, thumb, 10, 10), "b.jpg": (flipped(h, bits, start=100), thumb, 10, 10)},
    )
    assert bool(similar.similar_images(conn, scan_id).groups) is grouped


def test_a_drifting_burst_is_shown_but_only_partly_proposed(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    """A like B, B like C, but C not like A: C is in the group, never proposed."""
    scan_id, ids = analyse(["rafale/1.jpg", "rafale/2.jpg", "rafale/3.jpg"])
    h, thumb = random_hash(), random_thumb()
    sign(
        conn,
        scan_id,
        ids,
        {
            "rafale/1.jpg": (h, thumb, 4000, 3000),
            "rafale/2.jpg": (flipped(h, 16), thumb, 3000, 2000),
            "rafale/3.jpg": (flipped(flipped(h, 16), 16, start=16), thumb, 3000, 2000),
        },
    )
    [group] = similar.similar_images(conn, scan_id).groups
    assert paths(group)[0] == "rafale/1.jpg"
    close = {m.image.rel_path_display: m.close for m in group.members}
    assert close == {"rafale/1.jpg": True, "rafale/2.jpg": True, "rafale/3.jpg": False}
    assert [m.image.rel_path_display for m in group.removable] == ["rafale/2.jpg"]


def test_images_of_software_are_never_proposed(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    scan_id, ids = analyse(["Steam/steamapps/common/Jeu/fond.jpg", "Images/fond.jpg"])
    h, thumb = random_hash(), random_thumb()
    sign(
        conn,
        scan_id,
        ids,
        {
            "Steam/steamapps/common/Jeu/fond.jpg": (h, thumb, 1920, 1080),
            "Images/fond.jpg": (h, thumb, 1280, 720),
        },
    )
    [group] = similar.similar_images(conn, scan_id).groups
    assert group.keep.protection is not None and "logiciel" in group.keep.protection
    assert [m.image.rel_path_display for m in group.removable] == ["Images/fond.jpg"]


def test_hard_links_count_once(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    scan_id, ids = analyse(["a.jpg"], links={"lien-vers-a.jpg": "a.jpg"})
    h, thumb = random_hash(), random_thumb()
    sign(conn, scan_id, ids, {"a.jpg": (h, thumb, 10, 10), "lien-vers-a.jpg": (h, thumb, 10, 10)})
    report = similar.similar_images(conn, scan_id)
    assert (report.signed, report.groups) == (1, ())


def test_a_file_with_other_names_is_never_proposed(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    """Found by the audit of 2026-11-25: removing one name of a hard-linked file
    frees nothing, yet it was proposed and counted as space freed."""
    scan_id, ids = analyse(["grande.jpg", "petite.jpg"], links={"autre-nom.jpg": "petite.jpg"})
    h, thumb = random_hash(), random_thumb()
    sign(
        conn,
        scan_id,
        ids,
        {"grande.jpg": (h, thumb, 4000, 3000), "petite.jpg": (h, thumb, 800, 600)},
    )
    [group] = similar.similar_images(conn, scan_id).groups
    small = group.members[1]
    assert small.image.rel_path_display == "petite.jpg" and small.protection == similar.LINKED
    assert group.removable == () and group.recoverable == 0


def test_failures_and_other_versions_are_left_out(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    scan_id, ids = analyse(["a.jpg", "b.jpg", "c.jpg"])
    h, thumb = random_hash(), random_thumb()
    sign(conn, scan_id, ids, {"a.jpg": (h, thumb, 10, 10), "b.jpg": (h, thumb, 10, 10)})
    images_db.save(conn, scan_id, [], [images_db.Failure(ids["c.jpg"], "undecodable")], **LIMITS)  # type: ignore[arg-type]
    conn.execute("UPDATE image_sig SET algo = 99 WHERE entry_id = ?", (ids["b.jpg"],))
    report = similar.similar_images(conn, scan_id)
    assert (report.signed, report.failed, report.groups) == (1, 1, ())


def image(index: int, h: int, thumb: bytes) -> similar.Image:
    return similar.Image(
        entry_id=index,
        rel_path=f"{index}.jpg".encode(),
        rel_path_display=f"{index}.jpg",
        size=1000,
        mtime_ns=0,
        width=10,
        height=10,
        hash=h,
        thumb=thumb,
        thumb_sum=sum(thumb),
        thumb_squares=sum(v * v for v in thumb),
    )


def test_the_index_finds_exactly_what_comparing_every_pair_finds() -> None:
    """Random hashes, plus planted pairs at every distance around the limit:
    the pigeonhole index must miss none and invent none."""
    thumb = random_thumb()
    images = [image(i, random_hash(), thumb) for i in range(300)]
    for distance in range(36):
        base = random_hash()
        spread = RNG.sample(range(256), distance)
        other = base
        for bit in spread:
            other ^= 1 << bit
        images += [image(len(images), base, thumb), image(len(images) + 1, other, thumb)]
    # The hardest pairs: every one of the 16 pieces differs (one bit each, then
    # a second bit in some), so no piece is equal. Only the radius finds them.
    piece = 256 // similar.CHUNKS
    for distance in range(similar.CHUNKS, 33):
        base = random_hash()
        other = base
        for chunk in range(similar.CHUNKS):
            other ^= 1 << (chunk * piece)
        for chunk in range(distance - similar.CHUNKS):
            other ^= 1 << (chunk * piece + 1)
        images += [image(len(images), base, thumb), image(len(images) + 1, other, thumb)]
    found = set(similar.similar_pairs(images))
    brute = {
        (a, b)
        for a, b in itertools.combinations(range(len(images)), 2)
        if similar.similar(images[a], images[b])
    }
    assert found == brute
    assert len(brute) >= 31  # the planted pairs at 0..30 bits


def test_the_correlation_is_pearson_s() -> None:
    a, b = random_thumb(), nearby(random_thumb())
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    expected = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True)) / math.sqrt(
        sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b)
    )
    assert similar.correlation(image(0, 0, a), image(1, 0, b)) == pytest.approx(expected, abs=1e-12)
    flat = bytes([128]) * images_db.THUMB_BYTES
    assert similar.correlation(image(0, 0, flat), image(1, 0, flat)) == 0.0


def test_the_reason_is_what_put_the_kept_one_first() -> None:
    """A burst copied twice: same pixels and weight everywhere; the kept one is
    the oldest of the two copies of the best shot, whatever the other shots."""
    thumb = random_thumb()

    def shot(index: int, size: int, mtime: int) -> similar.Image:
        return dataclasses.replace(image(index, 0, thumb), size=size, mtime_ns=mtime)

    copies = [shot(1, 1300, 2024), shot(2, 1300, 2025), shot(3, 1200, 2024), shot(4, 1200, 2025)]
    group = similar.make_group(copies, Guarded(set(), {}))
    assert group.keep.image.entry_id == 1
    assert group.reasons == (
        "à résolution et poids égaux, la plus ancienne (probablement l'originale)",
    )
    heavier = similar.make_group([*copies[2:], shot(5, 1400, 2026)], Guarded(set(), {}))
    assert heavier.reasons == ("à résolution égale, le fichier le plus lourd",)


def test_identical_copies_are_told_apart_from_other_shots(
    conn: sqlite3.Connection, analyse: Callable[..., tuple[int, dict[str, int]]]
) -> None:
    """Found on real photos: most groups were the same photo saved twice.
    Without a duplicates search, the same size and signature make a likely copy;
    with one, only the same bytes do, whatever the signatures say."""
    same, other = os.urandom(4096), os.urandom(4096)
    names = ["2024/plage.jpg", "sauvegarde/plage.jpg", "2024/plage-2.jpg", "retouchée/plage.jpg"]
    contents = {names[0]: same, names[1]: same, names[2]: os.urandom(4096), names[3]: other}
    scan_id, ids = analyse(names, contents=contents)
    h, thumb = random_hash(), random_thumb()
    sign(
        conn,
        scan_id,
        ids,
        {
            names[0]: (h, thumb, 4000, 3000),
            names[1]: (h, thumb, 4000, 3000),  # the same file, saved twice
            names[2]: (flipped(h, 6), nearby(thumb), 4000, 3000),  # another shot
            names[3]: (h, thumb, 4000, 3000),  # same look and size, other bytes
        },
    )

    def marks() -> tuple[dict[str, tuple[str | None, str | None]], int]:
        [group] = similar.similar_images(conn, scan_id).groups
        found = {
            m.image.rel_path_display: (m.same_as and m.same_as.rel_path_display, m.proof)
            for m in group.members
        }
        return found, group.copies_recoverable

    guessed, guessed_bytes = marks()
    assert guessed[names[1]] == (names[0], similar.SAME_LOOK)
    assert guessed[names[3]] == (names[0], similar.SAME_LOOK)
    assert guessed[names[2]] == (None, None)
    assert guessed_bytes == 2 * 4096

    search_duplicates(conn, scan_id)
    proven, proven_bytes = marks()
    assert proven[names[1]] == (names[0], similar.SAME_BYTES)
    assert proven[names[3]] == (None, None)  # it only looked the same
    assert proven[names[2]] == (None, None)
    assert proven_bytes == 4096


def test_signatures_saved_while_reading_are_never_mixed(
    conn: sqlite3.Connection,
    analyse: Callable[..., tuple[int, dict[str, int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found by the audit of 2026-11-25: a batch saved between reading the
    version and the images gave groups newer than their version."""
    scan_id, ids = analyse(["a.jpg", "b.jpg", "c.jpg"])
    h, thumb = random_hash(), random_thumb()
    sign(conn, scan_id, ids, {"a.jpg": (h, thumb, 10, 10)})
    real_load = similar.load_images
    batches = iter([{"b.jpg": (h, thumb, 10, 10)}])

    def load_then_sign(conn_: sqlite3.Connection, scan: int) -> list[similar.Image]:
        loaded = real_load(conn_, scan)
        for batch in batches:  # once: another tab saves a batch meanwhile
            sign(conn, scan_id, ids, batch)
        return loaded

    monkeypatch.setattr(similar, "load_images", load_then_sign)
    report = similar.similar_images(conn, scan_id)
    assert report.version == similar.signatures_version(conn, scan_id)
    assert report.signed == 2 and len(report.groups) == 1  # the newer state, whole

    def always_newer(conn_: sqlite3.Connection, scan: int) -> list[similar.Image]:
        loaded = real_load(conn_, scan)
        conn.execute("UPDATE image_sig SET width = width + 1 WHERE scan_id = ?", (scan_id,))
        conn.execute(
            "INSERT OR REPLACE INTO image_sig SELECT * FROM image_sig WHERE scan_id = ?",
            (scan_id,),
        )
        return loaded

    monkeypatch.setattr(similar, "load_images", always_newer)
    with pytest.raises(similar.SignaturesChangedError):
        similar.similar_images(conn, scan_id)
