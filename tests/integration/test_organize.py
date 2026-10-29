"""Tidying (D-041): plan from the index, then the real scripts on a test tree."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.db.connection import connect
from treeaudit.organize.plan import DESTINATION, Plan, make_plan
from treeaudit.organize.script import tidy_script, undo_script
from treeaudit.roots.mountinfo import MountTable
from treeaudit.scanner.runner import ScanRequest, execute_scan

PHOTO_TIME = 1_563_000_000  # July 2019


def touch(path: Path, content: bytes = b"x", mtime: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    root = tmp_path / "Vieux disque"
    # An album: only photos, kept whole with its name.
    for i in range(3):
        touch(root / "Vacances Bretagne" / f"IMG_{i}.jpg", b"p" * (i + 1), PHOTO_TIME)
    touch(root / "Vacances Bretagne" / "Thumbs.db")  # clutter: still an album
    # A mixed folder: split by type.
    touch(root / "Bazar" / "facture.pdf", b"f")
    touch(root / "Bazar" / "chanson.mp3", b"m")
    touch(root / "Bazar" / "film.mkv", b"v", PHOTO_TIME)
    touch(root / "Bazar" / "photo.jpg", b"same name", PHOTO_TIME)
    touch(root / "Bazar" / ".config-appli", b"hidden")  # stays
    touch(root / "Bazar" / "Sous dossier" / "notes.txt", b"n")
    # A project: moved whole, never split.
    touch(root / "mon-site" / "package.json", b"{}")
    touch(root / "mon-site" / "src" / "index.js", b"js")
    touch(root / "mon-site" / "logo.png", b"png")
    # A game library: the whole Steam folder moves, never steamapps alone.
    touch(root / "Jeux" / "Steam" / "steamapps" / "common" / "jeu" / "jeu.bin", b"g")
    touch(root / "Jeux" / "Steam" / "steam.sh", b"#!/bin/sh")
    # Loose files at the top, one with a name already taken in the same folder.
    touch(root / "photo.jpg", b"other photo", PHOTO_TIME)
    touch(root / "$(touch PWNED) -rf.txt", b"hostile name")
    (root / "lien").symlink_to("Bazar")  # stays
    (root / "vide").mkdir()  # stays
    return root


def scan_and_plan(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> tuple[Plan, int]:
    config = AppConfig(
        data_dir=str(tmp_path / "data"), roots=(RootConfig(id="r", path=str(root), label="r"),)
    )
    outcome = execute_scan(
        config,
        ScanRequest(root=config.roots[0]),
        mounts=data_disk_table(str(tmp_path)),
        home=None,
        cancel=threading.Event(),
    )
    conn = connect(config.data_dir)
    try:
        start = conn.execute(
            "SELECT entry_id FROM entry WHERE scan_id = ? AND depth = 0", (outcome.scan_id,)
        ).fetchone()[0]
        return make_plan(conn, outcome.scan_id, start), outcome.scan_id
    finally:
        conn.close()


def snapshot(root: Path) -> dict[str, str]:
    """Every file (relative path -> SHA-256), links as their target."""
    found = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            found[rel] = "-> " + os.readlink(path)
        elif path.is_file():
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def run_script(text: str, where: Path) -> subprocess.CompletedProcess[str]:
    script = where / "script.sh"
    script.write_text(text, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - our own generated script, on a test tree
        ["bash", str(script)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_plan_keeps_albums_and_projects_whole(
    tmp_path: Path, messy: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    plan, _ = scan_and_plan(tmp_path, messy, data_disk_table)
    moves = {move.source: move for move in plan.moves}
    assert moves["Vacances Bretagne"].target == f"{DESTINATION}/Photos/2019/Vacances Bretagne"
    assert moves["Vacances Bretagne"].kind == "dir"
    assert moves["mon-site"].target == f"{DESTINATION}/Projets/mon-site"
    assert not any(source.startswith("mon-site/") for source in moves)  # never split
    assert moves["Jeux/Steam"].target == f"{DESTINATION}/Logiciels/Steam"
    assert not any(source.startswith("Jeux/Steam/") for source in moves)
    assert moves["Bazar/facture.pdf"].target == f"{DESTINATION}/Documents/facture.pdf"
    assert moves["Bazar/chanson.mp3"].target == f"{DESTINATION}/Musique/chanson.mp3"
    assert moves["Bazar/film.mkv"].target == f"{DESTINATION}/Vidéos/2019/film.mkv"
    assert moves["Bazar/Sous dossier"].category == "Documents"  # homogeneous too
    # Two photos named alike: the second one is renamed, and said so.
    photos = sorted((moves["photo.jpg"], moves["Bazar/photo.jpg"]), key=lambda m: m.target)
    assert [m.target for m in photos] == [
        f"{DESTINATION}/Photos/2019/photo (2).jpg",
        f"{DESTINATION}/Photos/2019/photo.jpg",
    ]
    assert [m.renamed for m in photos] == [True, False]
    kept = {k.path: k.reason for k in plan.kept}
    assert "Bazar/.config-appli" in kept and "lien" in kept and "vide" in kept
    assert plan.categories()["Photos"][0] == 3  # the album and two loose photos


def test_scripts_tidy_then_restore_everything_exactly(
    tmp_path: Path, messy: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    plan, scan_id = scan_and_plan(tmp_path, messy, data_disk_table)
    before = snapshot(messy)
    meta = {"title": "Vieux disque", "scan_id": scan_id, "date": "22 oct. 2026"}
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    tidied = run_script(tidy_script(plan, str(messy), **meta), scripts)
    assert tidied.returncode == 0, tidied.stderr
    assert f"{len(plan.moves)} élément(s) rangé(s), 0 ignoré(s)" in tidied.stdout
    after = snapshot(messy)
    assert f"{DESTINATION}/Photos/2019/Vacances Bretagne/IMG_0.jpg" in after
    assert f"{DESTINATION}/Projets/mon-site/src/index.js" in after
    assert f"{DESTINATION}/Documents/$(touch PWNED) -rf.txt" in after
    assert "Bazar/.config-appli" in after and after["lien"] == "-> Bazar"
    assert sorted(before.values()) == sorted(after.values())  # nothing lost, nothing changed
    assert not (scripts / "PWNED").exists() and not (messy / "PWNED").exists()

    restored = run_script(undo_script(plan, str(messy), **meta), scripts)
    assert restored.returncode == 0, restored.stderr
    assert snapshot(messy) == before
    assert not (messy / DESTINATION).exists()


def test_script_skips_what_changed_and_never_overwrites(
    tmp_path: Path, messy: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    plan, scan_id = scan_and_plan(tmp_path, messy, data_disk_table)
    (messy / "Bazar" / "facture.pdf").write_bytes(b"modified since the analysis")
    occupied = messy / DESTINATION / "Musique" / "chanson.mp3"
    touch(occupied, b"already there")
    meta = {"title": "t", "scan_id": scan_id, "date": "d"}
    result = run_script(tidy_script(plan, str(messy), **meta), tmp_path)
    assert result.returncode == 0
    assert "Bazar/facture.pdf (modifié depuis l'analyse)" in result.stderr
    assert "Bazar/chanson.mp3 (la place est déjà prise" in result.stderr
    assert (messy / "Bazar" / "facture.pdf").read_bytes() == b"modified since the analysis"
    assert occupied.read_bytes() == b"already there"
    assert (messy / "Bazar" / "chanson.mp3").exists()


def test_walking_down_uses_the_parent_index() -> None:
    """Regression: SQLite walked down with the inode index (minutes, not seconds)."""
    from treeaudit.db.connection import connect_memory
    from treeaudit.organize.plan import LOAD_SQL

    conn = connect_memory()
    try:
        plan = [
            row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + LOAD_SQL, (1, 1, 1, 1))
        ]
    finally:
        conn.close()
    assert "SEARCH e USING INDEX entry_by_parent (scan_id=? AND parent_id=?)" in plan
    assert not any("entry_by_inode" in step for step in plan), plan


# --- adjustments made by the user ----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a/b", "..", ".", ".cache", "-rf", "x" * 61, "a\nb", "a\\b", "  ", "nul\x00"],
)
def test_folder_names_are_checked(name: str) -> None:
    from treeaudit.organize.plan import AdjustmentError, adjustments_from_query

    with pytest.raises(AdjustmentError):
        adjustments_from_query([("nom_Photos", name)] if name.strip() else [("destination", "/")])


def test_adjustments_from_the_query_string() -> None:
    from treeaudit.organize.plan import AdjustmentError, adjustments_from_query

    adjust = adjustments_from_query(
        [
            ("exclure", "12"),
            ("categorie_7", "Archives"),
            ("categorie_8", ""),  # unchanged
            ("nom_Photos", " Images "),
            ("sous_9", "Factures EDF"),
            ("nom_Musique", "Musique"),  # same name: no change
            ("destination", "Classement 2026"),
            ("dir", "3"),  # not an adjustment: ignored
        ]
    )
    assert adjust.excluded == {12} and adjust.categories == {7: "Archives"}
    assert adjust.subfolders == {9: "Factures EDF"}
    assert adjust.names == {"Photos": "Images"} and adjust.destination == "Classement 2026"
    for bad in (
        [("exclure", "../1")],
        [("categorie_7", "Mes trucs")],  # not one of the categories
        [("categorie_x", "Archives")],
        [("nom_Inconnue", "x")],
        [("sous_9", "../../etc")],
        [("sous_x", "Factures")],
    ):
        with pytest.raises(AdjustmentError):
            adjustments_from_query(bad)


def test_adjusted_plan_and_scripts(
    tmp_path: Path, messy: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    from treeaudit.organize.plan import EXCLUDED, Adjustments

    plan, scan_id = scan_and_plan(tmp_path, messy, data_disk_table)
    ids = {move.source: move.entry_id for move in plan.moves}
    adjust = Adjustments(
        excluded=frozenset({ids["Vacances Bretagne"]}),
        categories={ids["Bazar/facture.pdf"]: "Archives"},
        subfolders={ids["Bazar/chanson.mp3"]: "Bretagne"},
        names={"Photos": "Images", "Archives": "Papiers"},
        destination="Classement",
    )
    conn = connect(str(tmp_path / "data"))
    try:
        start = conn.execute(
            "SELECT entry_id FROM entry WHERE scan_id = ? AND depth = 0", (scan_id,)
        ).fetchone()[0]
        adjusted = make_plan(conn, scan_id, start, adjust)
    finally:
        conn.close()
    moves = {move.source: move for move in adjusted.moves}
    assert "Vacances Bretagne" not in moves
    assert [(k.path, k.reason) for k in adjusted.excluded] == [("Vacances Bretagne", EXCLUDED)]
    assert moves["Bazar/facture.pdf"].target == "Classement/Papiers/facture.pdf"
    assert moves["Bazar/facture.pdf"].reason == "catégorie choisie par toi"
    assert moves["Bazar/film.mkv"].target == "Classement/Vidéos/2019/film.mkv"
    assert moves["Bazar/chanson.mp3"].target == "Classement/Musique/Bretagne/chanson.mp3"
    assert moves["photo.jpg"].target.startswith("Classement/Images/2019/")

    before = snapshot(messy)
    meta = {"title": "t", "scan_id": scan_id, "date": "d"}
    tidied = run_script(tidy_script(adjusted, str(messy), **meta), tmp_path)
    assert tidied.returncode == 0, tidied.stderr
    assert (messy / "Classement" / "Papiers" / "facture.pdf").is_file()
    assert (messy / "Vacances Bretagne" / "IMG_0.jpg").is_file()  # left in place
    assert not (messy / DESTINATION).exists()
    restored = run_script(undo_script(adjusted, str(messy), **meta), tmp_path)
    assert restored.returncode == 0, restored.stderr
    assert snapshot(messy) == before
    assert not (messy / "Classement").exists()


def test_a_name_alone_never_makes_a_program(
    tmp_path: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    root = tmp_path / "disque"
    touch(root / "notes" / "venv" / "important-notes.txt", b"n")  # only a note
    touch(root / "Images" / "venv" / "IMG_1.jpg", b"p", PHOTO_TIME)  # only photos
    touch(root / "Images" / "venv" / "IMG_2.jpg", b"q", PHOTO_TIME)
    touch(root / "outil" / "venv" / "pyvenv.cfg", b"home = /usr")  # a real one
    touch(root / "outil" / "venv" / "lib" / "site.py", b"x")
    touch(root / "outil" / "venv" / "lib" / "mod.so", b"y")
    plan, _ = scan_and_plan(tmp_path, root, data_disk_table)
    moves = {move.source: move for move in plan.moves}
    assert moves["notes"].category == "Documents"  # moved as a folder of documents
    assert moves["Images"].category == "Photos"
    assert moves["outil/venv"].category == "Logiciels"


def test_sizes_follow_the_explorer(
    tmp_path: Path, messy: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    plan, scan_id = scan_and_plan(tmp_path, messy, data_disk_table)
    conn = connect(str(tmp_path / "data"))
    try:
        from treeaudit.web.queries import all_children, start_node

        start = start_node(conn, scan_id)
        assert start is not None
        shown = {node.rel_path: node.size for node in all_children(conn, scan_id, start.entry_id)}
    finally:
        conn.close()
    for move in plan.moves:
        if "/" not in move.source:  # top-level elements are listed by the explorer too
            assert move.space == shown[move.source], move.source
    album = next(m for m in plan.moves if m.source == "Vacances Bretagne")
    assert album.space >= album.size  # a folder: the place it uses, not just its bytes


def test_a_sparse_file_counts_the_place_it_uses(
    tmp_path: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    """1 GiB that takes nothing on the disk is not 1 GiB to tidy (D-047)."""
    root = tmp_path / "disque"
    root.mkdir()
    with open(root / "vm.img", "wb") as image:
        image.truncate(1024**3)
    if os.stat(root / "vm.img").st_blocks * 512 > 1024**2:
        pytest.skip("this filesystem does not keep sparse files")
    plan, _ = scan_and_plan(tmp_path, root, data_disk_table)
    (move,) = plan.moves
    assert move.size == 1024**3  # what the script checks before moving
    assert move.space < 1024**2  # what the page adds up
    assert sum(total[2] for total in plan.categories().values()) == move.space


def test_a_backup_with_a_loose_requirements_txt_is_not_moved_whole(
    tmp_path: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    """D-049: only its projects move whole; its photos are sorted like the others."""
    root = tmp_path / "disque"
    backup = root / "sauvegarde_cle"
    touch(backup / "requirements.txt", b"requests")
    touch(backup / "photo.jpg", b"p", PHOTO_TIME)
    touch(backup / "agent" / "pyproject.toml", b"[project]")
    touch(root / "script" / "requirements.txt", b"rich")
    touch(root / "script" / "run.py")
    plan, _ = scan_and_plan(tmp_path, root, data_disk_table)
    moves = {move.source: move.target for move in plan.moves}
    assert "sauvegarde_cle" not in moves
    assert moves["sauvegarde_cle/agent"] == f"{DESTINATION}/Projets/agent"
    assert moves["sauvegarde_cle/photo.jpg"].startswith(f"{DESTINATION}/Photos/2019/")
    assert moves["script"] == f"{DESTINATION}/Projets/script"
