"""Image preview (D-046): exact bytes, type from the content, never a swapped file."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.duplicates.reader import Expected
from treeaudit.preview.images import MAX_PREVIEW_SIZE, PreviewError, read_image, sniff
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.registry import evaluate_roots
from treeaudit.web.app import create_app

CSRF = {"X-TreeAudit-Request": "1"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + os.urandom(64)
JPEG = b"\xff\xd8\xff\xe0" + os.urandom(64)


@pytest.mark.parametrize(
    ("head", "media_type"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (b"GIF89a" + bytes(10), "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"BM" + bytes(30), "image/bmp"),
        (b"\x00\x00\x00\x1cftypavif" + bytes(8), "image/avif"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', None),
        (b"<html><script>alert(1)</script>", None),
        (b"just text", None),
        (b"", None),
    ],
)
def test_type_comes_from_the_content(head: bytes, media_type: str | None) -> None:
    assert sniff(head[:32]) == media_type


def expected_of(path: Path) -> Expected:
    st = os.lstat(path)
    return Expected(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def root_id(path: Path) -> tuple[int, int]:
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def test_reads_the_exact_bytes(tmp_path: Path) -> None:
    (tmp_path / "Photos").mkdir()
    image = tmp_path / "Photos" / "a.png"
    image.write_bytes(PNG)
    got = read_image(str(tmp_path), root_id(tmp_path), b"Photos/a.png", expected_of(image))
    assert (got.content, got.media_type) == (PNG, "image/png")


def test_a_modified_file_is_not_shown(tmp_path: Path) -> None:
    image = tmp_path / "a.png"
    image.write_bytes(PNG)
    expected = expected_of(image)
    image.write_bytes(PNG + b"more")
    with pytest.raises(PreviewError, match="modifié"):
        read_image(str(tmp_path), root_id(tmp_path), b"a.png", expected)


def test_a_folder_swapped_for_a_symlink_is_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "Photos").mkdir(parents=True)
    image = root / "Photos" / "a.png"
    image.write_bytes(PNG)
    expected = expected_of(image)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.link(image, outside / "a.png")  # same inode: only the symlink can betray it
    (root / "Photos" / "a.png").unlink()
    (root / "Photos").rmdir()
    (root / "Photos").symlink_to(outside)
    with pytest.raises(PreviewError):
        read_image(str(root), root_id(root), b"Photos/a.png", expected)


def test_private_folders_and_big_files_are_never_read(tmp_path: Path) -> None:
    (tmp_path / ".ssh").mkdir()
    secret = tmp_path / ".ssh" / "a.png"
    secret.write_bytes(PNG)
    with pytest.raises(PreviewError, match="privé"):
        read_image(
            str(tmp_path),
            root_id(tmp_path),
            b".ssh/a.png",
            expected_of(secret),
            private_paths=frozenset({b".ssh"}),
        )
    big = Expected(1, 1, MAX_PREVIEW_SIZE + 1, 0)
    with pytest.raises(PreviewError, match="trop grande"):
        read_image(str(tmp_path), root_id(tmp_path), b"big.jpg", big)


def test_a_text_named_jpg_is_not_an_image(tmp_path: Path) -> None:
    fake = tmp_path / "fake.jpg"
    fake.write_bytes(b"<script>alert(1)</script>")
    with pytest.raises(PreviewError, match="pas une image"):
        read_image(str(tmp_path), root_id(tmp_path), b"fake.jpg", expected_of(fake))


# --- through the web server ------------------------------------------------------------------


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    root = tmp_path / "disque"
    (root / "Photos").mkdir(parents=True)
    for i in range(45):
        (root / "Photos" / f"IMG_{i:02}.png").write_bytes(PNG)
    (root / "Photos" / "faux.jpg").write_bytes(b"<svg onload=alert(1)>")
    (root / "Photos" / "notes.txt").write_bytes(b"texte")
    return root


@pytest.fixture
def client(
    tmp_path: Path, photos: Path, data_disk_table: Callable[..., MountTable]
) -> Iterator[TestClient]:
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="d", path=str(photos), label="Disque"),),
    )
    mounts = data_disk_table(str(tmp_path))
    app = create_app(
        config, evaluate_roots(config, mounts, home=None), mounts=lambda: mounts, home=lambda: None
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        response = test_client.post(
            "/api/scans", json={"root_id": "d", "subpath": ""}, headers=CSRF
        )
        assert response.status_code == 202
        job = app.state.jobs.wait()
        assert job is not None and not job.running
        yield test_client
    app.state.jobs.cancel()
    app.state.jobs.wait()


def entry_ids(client: TestClient) -> tuple[int, dict[str, int]]:
    scan_id = client.get("/api/jobs/current").json()["job"]["scan_id"]
    page = client.get(f"/scans/{scan_id}/space").text
    folder = int(re.search(r'space\?dir=(\d+)[^"]*">📁 Photos', page).group(1))  # type: ignore[union-attr]
    listing = client.get(f"/scans/{scan_id}/space?dir={folder}").text
    ids = {name: int(i) for i, name in re.findall(r'entries/(\d+)">📄 ([^<]+)</a>', listing)}
    return scan_id, {"folder": folder, **ids}


def test_the_image_is_served_safely(client: TestClient) -> None:
    scan_id, ids = entry_ids(client)
    response = client.get(f"/api/scans/{scan_id}/entries/{ids['IMG_00.png']}/image")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"
    fake = client.get(f"/api/scans/{scan_id}/entries/{ids['faux.jpg']}/image")
    assert fake.status_code == 409 and "pas une image" in fake.text
    assert client.get(f"/api/scans/{scan_id}/entries/{ids['notes.txt']}/image").status_code == 422
    assert client.get(f"/api/scans/{scan_id}/entries/{ids['folder']}/image").status_code == 422
    assert client.get(f"/api/scans/{scan_id}/entries/999999/image").status_code == 404


def test_fiche_and_gallery(client: TestClient) -> None:
    scan_id, ids = entry_ids(client)
    fiche = client.get(f"/scans/{scan_id}/entries/{ids['IMG_00.png']}").text
    assert f'src="/api/scans/{scan_id}/entries/{ids["IMG_00.png"]}/image"' in fiche
    assert "<img" not in client.get(f"/scans/{scan_id}/entries/{ids['notes.txt']}").text

    listing = client.get(f"/scans/{scan_id}/space?dir={ids['folder']}").text
    assert "🖼️ Galerie (46 images)" in listing and "<img" not in listing
    gallery = client.get(f"/scans/{scan_id}/space?dir={ids['folder']}&vue=galerie").text
    assert gallery.count("<img ") == 40  # few per page: the browser decodes each one
    assert "notes.txt" not in gallery and "Page 1 sur 2" in gallery
    assert 'loading="lazy"' in gallery
    second = client.get(f"/scans/{scan_id}/space?dir={ids['folder']}&vue=galerie&page=2").text
    assert second.count("<img ") == 6
    assert client.get(f"/scans/{scan_id}/space?dir={ids['folder']}&vue=x").status_code == 422


def test_many_images_at_once(client: TestClient) -> None:
    """A gallery asks for its images in parallel (found while exploring)."""
    from concurrent.futures import ThreadPoolExecutor

    scan_id, ids = entry_ids(client)
    urls = [f"/api/scans/{scan_id}/entries/{i}/image" for n, i in ids.items() if n.endswith(".png")]
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda url: client.get(url).status_code, urls * 3))
    assert codes == [200] * len(urls) * 3
