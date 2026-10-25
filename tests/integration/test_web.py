"""Web interface: pages, JSON API, jobs, commands and exports, with hostile names."""

from __future__ import annotations

import csv
import html
import io
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from treeaudit.archive import destination
from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.db.connection import database_path
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.registry import evaluate_roots
from treeaudit.roots.validation import RejectReason
from treeaudit.web import explorer
from treeaudit.web import jobs as jobs_module
from treeaudit.web.app import create_app

CSRF = {"X-TreeAudit-Request": "1"}
XSS = "<img src=x onerror=alert(1)>"
XSS_DIR = '"><svg onload=alert(2)>'  # no "/": it must stay one name
FORMULA = "=HYPERLINK(1)"
DISPLAY = "/media/My Disk"


def touch(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    duplicate = b"same content " * 200
    touch(base / "photos" / "a.jpg", duplicate)
    touch(base / "photos" / f"{XSS}.jpg", duplicate)
    touch(base / "photos" / f"{FORMULA}.jpg", duplicate)
    touch(base / XSS_DIR / "package.json", b"{}")
    touch(base / XSS_DIR / "node_modules" / "dep" / "index.js", b"x")
    touch(base / "app" / "package.json", b"{}")
    touch(base / "app" / "node_modules" / "dep" / "index.js", b"x")
    touch(base / "=SUM(1).txt", b"formula at the start of the path")
    (base / "link-to-tmp").symlink_to(tmp_path)
    return base


@pytest.fixture
def client(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> Iterator[TestClient]:
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(
            RootConfig(id="main", path=str(root), label="Disque <test>", display_path=DISPLAY),
            RootConfig(id="gone", path=str(tmp_path / "missing"), label="Absente"),
        ),
    )
    mounts = data_disk_table(str(tmp_path))
    statuses = evaluate_roots(config, mounts, home=None)
    assert [s.accepted for s in statuses] == [True, False]
    app = create_app(config, statuses, mounts=lambda: mounts, home=lambda: None)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client
    app.state.jobs.cancel()
    app.state.jobs.wait()


def run_job(client: TestClient, url: str, body: dict[str, object]) -> dict[str, object]:
    response = client.post(url, json=body, headers=CSRF)
    assert response.status_code == 202, response.text
    job = client.app.state.jobs.wait()  # type: ignore[attr-defined]
    assert job is not None and not job.running
    snapshot = client.get("/api/jobs/current").json()["job"]
    assert snapshot["error"] is None, snapshot
    return snapshot  # type: ignore[no-any-return]


@pytest.fixture
def scanned(client: TestClient) -> int:
    snapshot = run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})
    assert snapshot["status"] == "complete"
    return int(snapshot["scan_id"])  # type: ignore[call-overload]


class InlineCodeFinder(HTMLParser):
    """Collects what a strict CSP would block: inline scripts, handlers, styles."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script":
            self._in_script = "src" not in names
        if tag in ("img", "svg", "iframe", "object", "embed"):
            self.problems.append(f"unexpected <{tag}>")
        for name in names:
            if name.startswith("on") or name == "style":
                self.problems.append(f"<{tag} {name}=...>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.problems.append("inline <script> content")


def assert_safe_html(text: str) -> None:
    """Hostile names are escaped, and no page carries inline code."""
    finder = InlineCodeFinder()
    finder.feed(text)
    assert finder.problems == []


# --- pages ------------------------------------------------------------------------------


def test_index_and_security_headers(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Analyser un dossier" in response.text
    assert "script-src" not in response.headers["content-security-policy"]  # default-src 'self'
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert_safe_html(response.text)


def test_static_files(client: TestClient) -> None:
    assert client.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/static/app.css").headers["content-type"].startswith("text/css")
    script = client.get("/static/app.js").text
    assert not re.search(r"\.(inner|outer)HTML\s*=|insertAdjacentHTML|document\.write", script)


def test_theme_choice(client: TestClient) -> None:
    page = client.get("/").text
    # Applied in <head>, before the stylesheet and without "defer": no flash.
    head = page.split("</head>")[0]
    assert '<script src="/static/theme.js"></script>' in head
    assert head.index("theme.js") < head.index("app.css")
    assert "data-theme-toggle" in page
    theme = client.get("/static/theme.js")
    assert theme.headers["content-type"].startswith("text/javascript")
    assert "localStorage" in theme.text and "try" in theme.text
    css = client.get("/static/app.css").text
    assert ':root[data-theme="dark"]' in css
    assert ':root:not([data-theme="light"])' in css


def test_foreign_host_rejected_on_pages(client: TestClient) -> None:
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_folder_picker(client: TestClient) -> None:
    response = client.get("/scan/new?root=main")
    assert response.status_code == 200
    assert "photos" in response.text
    assert "link-to-tmp" not in response.text  # symlinks are never proposed
    assert "&lt;svg onload=alert(2)&gt;" in response.text
    assert_safe_html(response.text)
    inside = client.get("/scan/new", params={"root": "main", "path": "photos"})
    assert inside.status_code == 200


@pytest.mark.parametrize(
    ("path", "status"),
    [("../..", 422), ("/etc", 422), ("photos/../..", 422), ("link-to-tmp", 404), ("nope", 404)],
)
def test_folder_picker_refuses_escapes(client: TestClient, path: str, status: int) -> None:
    page = client.get("/scan/new", params={"root": "main", "path": path})
    assert page.status_code == status
    assert "passwd" not in page.text  # nothing outside the root is ever listed
    api = client.get("/api/roots/main/folders", params={"path": path})
    assert api.status_code == status


def test_folder_api(client: TestClient) -> None:
    data = client.get("/api/roots/main/folders").json()
    assert "photos" in data["folders"] and "link-to-tmp" not in data["folders"]
    assert client.get("/api/roots/gone/folders").status_code == 409
    assert client.get("/api/roots/unknown/folders").status_code == 404


def test_scan_requires_csrf_header(client: TestClient) -> None:
    body = {"root_id": "main", "subpath": ""}
    assert client.post("/api/scans", json=body).status_code == 403
    assert client.post("/api/scans", data=body, headers=CSRF).status_code == 403  # not JSON


@pytest.mark.parametrize(
    "body",
    [
        {"root_id": "gone", "subpath": ""},
        {"root_id": "../etc", "subpath": ""},
        {"root_id": "main", "subpath": "../.."},
        {"root_id": "main", "subpath": "/etc"},
    ],
)
def test_scan_refuses_bad_requests(client: TestClient, body: dict[str, str]) -> None:
    assert client.post("/api/scans", json=body, headers=CSRF).status_code in (404, 409, 422)


def test_one_job_at_a_time(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow_scan(*args: object, **kwargs: object) -> None:
        release.wait(5)
        raise jobs_module.JobError("stopped by the test")

    monkeypatch.setattr(jobs_module, "execute_scan", slow_scan)
    body = {"root_id": "main", "subpath": ""}
    assert client.post("/api/scans", json=body, headers=CSRF).status_code == 202
    busy = client.post("/api/scans", json=body, headers=CSRF)
    assert busy.status_code == 409
    assert client.post("/api/jobs/current/cancel", json={}, headers=CSRF).json() == {
        "cancelled": True
    }
    release.set()
    client.app.state.jobs.wait()  # type: ignore[attr-defined]
    assert client.get("/api/jobs/current").json()["job"]["error"] == "stopped by the test"


def test_scan_page_and_explorer(client: TestClient, scanned: int) -> None:
    page = client.get(f"/scans/{scanned}")
    assert page.status_code == 200
    assert "photos" in page.text
    assert_safe_html(page.text)
    assert "Où va la place" in page.text and "À nettoyer" in page.text
    space = client.get(f"/scans/{scanned}/space")
    assert_safe_html(space.text)
    match = re.search(
        rf'href="/scans/{scanned}/space\?dir=(\d+)&amp;sort=size">📁 photos', space.text
    )
    assert match is not None
    inside = client.get(f"/scans/{scanned}/space?dir={match.group(1)}")
    assert inside.status_code == 200 and "a.jpg" in inside.text
    assert_safe_html(inside.text)
    assert client.get(f"/scans/{scanned}/space?dir=999999").status_code == 404
    assert client.get("/scans/999").status_code == 404
    assert "Oups" in client.get("/scans/999").text


def test_job_page(client: TestClient, scanned: int) -> None:
    page = client.get("/jobs/current")
    assert page.status_code == 200
    assert_safe_html(page.text)


# --- parasites -------------------------------------------------------------------------------


def test_parasites_page_and_command(client: TestClient, scanned: int) -> None:
    page = client.get(f"/scans/{scanned}/parasites")
    assert page.status_code == 200
    assert "Dépendances Node.js" in page.text
    assert_safe_html(page.text)
    numbers = [int(n) for n in re.findall(r'data-pick="(\d+)"', page.text)]
    assert len(numbers) == 2

    url = f"/api/scans/{scanned}/parasites/command"
    assert client.post(url, json={"numbers": numbers}).status_code == 403
    response = client.post(url, json={"numbers": numbers, "action": "trash"}, headers=CSRF)
    assert response.status_code == 200
    command = response.json()["command"]
    assert command.startswith("gio trash -- \\\n  ")
    assert f"'{DISPLAY}/app/node_modules'" in command
    # The hostile folder name reaches the command literally, single-quoted.
    assert f"'{DISPLAY}/\"><svg onload=alert(2)>/node_modules'" in command
    bad = client.post(url, json={"numbers": [999]}, headers=CSRF)
    assert bad.status_code == 422
    wrong_action = client.post(url, json={"numbers": numbers, "action": "rm"}, headers=CSRF)
    assert wrong_action.status_code == 422


# --- duplicates ------------------------------------------------------------------------------


def test_duplicates_page_and_command(client: TestClient, scanned: int) -> None:
    snapshot = run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    run_id = snapshot["run_id"]
    page = client.get(f"/dupes/{run_id}")
    assert page.status_code == 200
    assert "exemplaires" in page.text
    assert_safe_html(page.text)
    # Full host path: the disk in muted text, then the path inside it.
    assert f'<span class="muted">{DISPLAY}/</span>photos/a.jpg' in page.text
    assert f'<span class="muted">{DISPLAY}/</span>photos/&lt;img src=x' in page.text

    url = f"/api/dupes/{run_id}/command"
    suggested = client.post(url, json={"groups": [1]}, headers=CSRF).json()
    assert suggested["count"] == 2
    keep_other = client.post(url, json={"groups": [1], "keep": {"1": 1}}, headers=CSRF).json()
    assert keep_other["count"] == 2
    assert "a.jpg" in keep_other["command"]  # the suggested copy is now removed instead
    assert client.post(url, json={"groups": [7]}, headers=CSRF).status_code == 422
    assert client.post(url, json={"groups": [1], "keep": {"1": 9}}, headers=CSRF).status_code == 422
    archive = client.post(url, json={"groups": [1], "action": "archive"}, headers=CSRF)
    assert archive.status_code == 422  # archiving duplicates makes no sense


# --- search -------------------------------------------------------------------------------------


def test_search_page(client: TestClient, scanned: int) -> None:
    page = client.get(f"/scans/{scanned}/search", params={"go": "1", "ext": "jpg"})
    assert page.status_code == 200
    assert "3 résultats" in page.text
    simple = client.get(f"/scans/{scanned}/search", params={"q": "nothing-like-this"})
    assert simple.status_code == 200 and "0 résultat" in simple.text
    simple = client.get(f"/scans/{scanned}/search", params={"q": "A.JPG"})
    assert "1 résultat" in simple.text  # any case
    assert_safe_html(page.text)
    reflected = client.get(f"/scans/{scanned}/search", params={"go": "1", "glob": XSS})
    assert_safe_html(reflected.text)
    refused = client.get(f"/scans/{scanned}/search", params={"go": "1", "regex": "(a+)+"})
    assert "refusée" in refused.text
    invalid = client.get(f"/scans/{scanned}/search", params={"go": "1", "regex": "[a"})
    assert "invalide" in invalid.text


# --- exports --------------------------------------------------------------------------------------


def read_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_csv_exports_neutralise_formulas(client: TestClient, scanned: int) -> None:
    response = client.get(f"/export/scans/{scanned}/entries.csv")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    paths = [row[0] for row in read_csv(response.text)[1:]]
    assert "'=SUM(1).txt" in paths  # neutralised: the cell started with "="
    assert "=SUM(1).txt" not in paths
    assert f"photos/{FORMULA}.jpg" in paths  # "=" inside the path is harmless
    snapshot = run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    dupes_rows = read_csv(client.get(f"/export/dupes/{snapshot['run_id']}.csv").text)
    assert all(not cell.startswith(("=", "+", "@")) for row in dupes_rows for cell in row)
    search = client.get(
        f"/export/scans/{scanned}/search.csv", params={"go": "1", "name": f"{FORMULA}.jpg"}
    )
    assert any(row[0] == f"photos/{FORMULA}.jpg" for row in read_csv(search.text))


def test_formula_at_the_start_of_a_cell_is_neutralised() -> None:
    from treeaudit.web.exports import csv_cell

    for value in ("=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(1)", "\tx", "\rx"):
        assert csv_cell(value) == "'" + value
    assert csv_cell("photos/=x") == "photos/=x"
    assert csv_cell(42) == 42


def test_json_exports(client: TestClient, scanned: int) -> None:
    scan = client.get(f"/export/scans/{scanned}.json").json()
    assert scan["scan"]["status"] == "complete"
    assert "root_path" not in scan["scan"]
    parasites = client.get(f"/export/scans/{scanned}/parasites.json").json()
    assert len(parasites["findings"]) == 2
    assert client.get("/export/scans/999.json").status_code == 404


def test_rejected_root_listed_but_unusable(client: TestClient) -> None:
    roots = client.get("/api/roots").json()["roots"]
    gone = next(r for r in roots if r["id"] == "gone")
    assert gone["accepted"] is False and gone["reason"] == RejectReason.NOT_FOUND.value
    assert client.get("/scan/new?root=gone").status_code == 404


def test_pages_never_show_the_container_root_path(
    client: TestClient, scanned: int, root: Path
) -> None:
    run = run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    urls = [
        "/",
        f"/scans/{scanned}",
        f"/scans/{scanned}/parasites",
        f"/scans/{scanned}/space",
        f"/scans/{scanned}/entries/1",
        f"/dupes/{run['run_id']}",
        "/scan/new?root=main",
    ]
    for url in urls:
        page = client.get(url)
        assert page.status_code == 200, url
        assert str(root) not in page.text, url


@pytest.mark.parametrize(
    "html",
    [
        f"<p>{XSS}</p>",
        f"<p>{XSS_DIR}</p>",
        "<script>alert(1)</script>",
        '<a href="#" onclick="x()">x</a>',
        '<p style="color:red">x</p>',
    ],
)
def test_inline_code_finder_catches_injections(html: str) -> None:
    with pytest.raises(AssertionError):
        assert_safe_html(html)


# --- archives (D-032) ----------------------------------------------------------------------


def test_archive_disabled_without_archive_dir(client: TestClient) -> None:
    page = client.get("/scan/new?root=main")
    assert "Archivage désactivé" in page.text
    assert "data-start-archive" not in page.text
    response = client.post("/api/archives", json={"root_id": "main"}, headers=CSRF)
    assert response.status_code == 409
    assert "archivage indisponible" in response.json()["detail"]


@pytest.fixture
def archive_client(
    tmp_path: Path,
    root: Path,
    data_disk_table: Callable[..., MountTable],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    # pytest's tmp_path lives below /tmp, refused for archives: lift that one rule.
    trees = tuple(tree for tree in destination.SYSTEM_TREES if tree != "/tmp")
    monkeypatch.setattr(destination, "SYSTEM_TREES", trees)
    (tmp_path / "archives").mkdir()
    (root / os.fsdecode(b"caf\xe9")).mkdir()  # not valid UTF-8
    (root / os.fsdecode(b"caf\xe9") / "a.txt").write_bytes(b"a")
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(root), label="Disque", display_path=DISPLAY),),
        archive_dir=str(tmp_path / "archives"),
    )
    mounts = data_disk_table(str(tmp_path))
    statuses = evaluate_roots(config, mounts, home=None)
    app = create_app(config, statuses, mounts=lambda: mounts, home=lambda: None)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client
    app.state.jobs.cancel()
    app.state.jobs.wait()


def archive_names(client: TestClient) -> list[str]:
    archive_dir = client.app.state.config.archive_dir  # type: ignore[attr-defined]
    return sorted(os.listdir(archive_dir))


def test_archive_from_the_folder_picker(archive_client: TestClient) -> None:
    page = archive_client.get("/scan/new?root=main&path=photos")
    assert "data-start-archive" in page.text
    assert "Archiver « photos »" in page.text
    assert_safe_html(page.text)

    body = {"root_id": "main", "subpath": "photos"}
    assert archive_client.post("/api/archives", json=body).status_code == 403  # no CSRF header
    snapshot = run_job(archive_client, "/api/archives", body)
    assert snapshot["kind"] == "archive"
    assert snapshot["status"] == "complete"
    result = snapshot["archive"]
    assert isinstance(result, dict)
    assert result["verified"] and result["same_disk"]
    assert str(result["path"]).endswith(".tar.gz")
    assert result["problems"] == []
    [name] = archive_names(archive_client)
    assert name.startswith("photos-") and str(result["path"]).endswith(name)

    job_page = archive_client.get("/jobs/current")
    assert "data-archive-result" in job_page.text
    assert_safe_html(job_page.text)


@pytest.mark.parametrize(
    "body",
    [
        {"root_id": "main", "subpath": "../.."},
        {"root_id": "main", "subpath": "/etc"},
        {"root_id": "nope"},
        {"root_id": "BAD ID"},
    ],
)
def test_archive_refuses_bad_requests(archive_client: TestClient, body: dict[str, str]) -> None:
    response = archive_client.post("/api/archives", json=body, headers=CSRF)
    assert response.status_code in (404, 409, 422)
    assert archive_names(archive_client) == []


def test_archive_from_an_analysis_with_hostile_names(archive_client: TestClient) -> None:
    scan = run_job(archive_client, "/api/scans", {"root_id": "main", "subpath": ""})
    scan_id = int(scan["scan_id"])  # type: ignore[call-overload]
    data_dir = archive_client.app.state.config.data_dir  # type: ignore[attr-defined]
    conn = sqlite3.connect(database_path(data_dir))
    try:
        rows = {
            bytes(rel): (entry_id, kind)
            for entry_id, rel, kind in conn.execute(
                "SELECT entry_id, rel_path, kind FROM entry WHERE scan_id = ?", (scan_id,)
            )
        }
    finally:
        conn.close()

    space = archive_client.get(f"/scans/{scan_id}/space?dir={rows[XSS_DIR.encode()][0]}")
    assert "Archiver ce dossier" in space.text
    assert_safe_html(space.text)

    for raw in (XSS_DIR.encode(), b"caf\xe9"):
        entry_id = rows[raw][0]
        url = f"/api/scans/{scan_id}/entries/{entry_id}/archive"
        snapshot = run_job(archive_client, url, {})
        assert snapshot["status"] == "complete", snapshot
        assert_safe_html(archive_client.get("/jobs/current").text)
    assert len(archive_names(archive_client)) == 2

    file_id = rows[b"photos/a.jpg"][0]
    url = f"/api/scans/{scan_id}/entries/{file_id}/archive"
    assert archive_client.post(url, json={}, headers=CSRF).status_code == 422
    url = f"/api/scans/{scan_id}/entries/999999/archive"
    assert archive_client.post(url, json={}, headers=CSRF).status_code == 404


def test_archive_errors_are_plain_french(
    archive_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from treeaudit.archive.output import ArchiveOutput

    monkeypatch.setattr(ArchiveOutput, "free_bytes", lambda self: 10)
    response = archive_client.post(
        "/api/archives", json={"root_id": "main", "subpath": "photos"}, headers=CSRF
    )
    assert response.status_code == 202
    archive_client.app.state.jobs.wait()  # type: ignore[attr-defined]
    snapshot = archive_client.get("/api/jobs/current").json()["job"]
    assert snapshot["error"].startswith("espace insuffisant dans le dossier d'archives")
    assert archive_names(archive_client) == []


def test_archive_refuses_a_folder_replaced_since_the_analysis(
    archive_client: TestClient, root: Path
) -> None:
    scan = run_job(archive_client, "/api/scans", {"root_id": "main", "subpath": ""})
    scan_id = int(scan["scan_id"])  # type: ignore[call-overload]
    data_dir = archive_client.app.state.config.data_dir  # type: ignore[attr-defined]
    conn = sqlite3.connect(database_path(data_dir))
    try:
        (entry_id,) = conn.execute(
            "SELECT entry_id FROM entry WHERE scan_id = ? AND rel_path = ?", (scan_id, b"photos")
        ).fetchone()
    finally:
        conn.close()
    (root / "photos").rename(root / "photos-old")
    (root / "photos").mkdir()  # same relative path, another folder
    url = f"/api/scans/{scan_id}/entries/{entry_id}/archive"
    snapshot = run_job(archive_client, url, {})
    assert snapshot["status"] == "failed"
    assert "n'est plus celui de l'analyse" in str(snapshot["archive"]["failure"])  # type: ignore[index]
    assert archive_names(archive_client) == []


# --- external disks (D-033) ---------------------------------------------------------------


def test_picker_suggests_enabling_external_disks(client: TestClient) -> None:
    page = client.get("/scan/new")
    assert "détection des disques externes est désactivée" in page.text


@pytest.fixture
def disks_client(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> Iterator[TestClient]:
    from treeaudit.roots.external import DiskReason, ExternalDisk, disk_root_id
    from treeaudit.roots.registry import RootStatus
    from treeaudit.roots.validation import RejectReason

    disk = RootConfig(
        id=disk_root_id("/run/media/me/USB"),
        path=str(root),
        label=XSS_DIR,
        display_path="/run/media/me/USB",
        external=True,
    )
    unmounted = RootConfig(id="main", path="/audit/roots/main", label="Racine principale")
    config = AppConfig(data_dir=str(tmp_path / "data"), roots=(unmounted, disk))
    statuses = (
        RootStatus(root=unmounted, reason=RejectReason.NOT_MOUNTED),
        RootStatus(root=disk),
    )
    detected = [
        ExternalDisk("USB", "/run/media/me/USB", disk.id, "exfat"),
        ExternalDisk(
            "Data", "/mnt/Data", disk_root_id("/mnt/Data"), "ext4", DiskReason.SYSTEM_DISK
        ),
    ]
    mounts = data_disk_table(str(tmp_path))
    app = create_app(
        config, statuses, mounts=lambda: mounts, home=lambda: None, external_disks=detected
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client
    app.state.jobs.cancel()
    app.state.jobs.wait()


def test_picker_lists_external_disks(disks_client: TestClient) -> None:
    page = disks_client.get("/scan/new")
    assert page.status_code == 200
    assert "Disques externes" in page.text
    assert "/run/media/me/USB" in page.text
    assert "utilisés sur" in page.text
    assert "Disques non proposés (1)" in page.text
    assert "Data : ce disque contient le système" in page.text
    assert "détection des disques externes est désactivée" not in page.text
    assert_safe_html(page.text)  # the disk label is a hostile name here

    home = disks_client.get("/")
    assert "Analyser un dossier" in home.text
    assert "refusés par la configuration" not in home.text  # not mounted = not configured


def test_external_disk_can_be_scanned(disks_client: TestClient) -> None:
    from treeaudit.roots.external import disk_root_id

    root_id = disk_root_id("/run/media/me/USB")
    page = disks_client.get(f"/scan/new?root={root_id}")
    assert page.status_code == 200
    assert "data-start-scan" in page.text
    snapshot = run_job(disks_client, "/api/scans", {"root_id": root_id, "subpath": "photos"})
    assert snapshot["status"] == "complete"
    response = disks_client.post("/api/scans", json={"root_id": "main"}, headers=CSRF)
    assert response.status_code == 409  # not mounted: refused


# --- "Mes archives" (D-034) ------------------------------------------------------------------


def test_archives_page(archive_client: TestClient) -> None:
    archive_dir = Path(archive_client.app.state.config.archive_dir)  # type: ignore[attr-defined]
    empty = archive_client.get("/archives")
    assert empty.status_code == 200
    assert "Aucune archive pour l'instant" in empty.text
    assert 'href="/archives"' in archive_client.get("/").text

    run_job(archive_client, "/api/archives", {"root_id": "main", "subpath": "photos"})
    (archive_dir / f"{XSS}-20261021-INCOMPLET.tar.gz").write_bytes(b"x")
    (archive_dir / ".old-20261020.tar.gz.partial").write_bytes(b"x")
    (archive_dir / "notes.txt").write_bytes(b"not an archive")
    (archive_dir / "link.tar.gz").symlink_to("/etc/passwd")
    page = archive_client.get("/archives")
    assert "photos-" in page.text
    assert "1 archive(s) inachevée(s)" in page.text
    assert "⚠ " in page.text and "archive incomplète" in page.text
    assert "notes.txt" not in page.text and "link.tar.gz" not in page.text
    assert_safe_html(page.text)


def test_archives_page_without_archive_dir(client: TestClient) -> None:
    page = client.get("/archives")
    assert page.status_code == 200
    assert "Archivage indisponible" in page.text
    assert 'href="/archives"' not in client.get("/").text


# --- explorer and basket (D-036) -----------------------------------------------------------


def entry_ids(client: TestClient, scan_id: int) -> dict[bytes, int]:
    data_dir = client.app.state.config.data_dir  # type: ignore[attr-defined]
    conn = sqlite3.connect(database_path(data_dir))
    try:
        return {
            bytes(rel): entry_id
            for entry_id, rel in conn.execute(
                "SELECT entry_id, rel_path FROM entry WHERE scan_id = ?", (scan_id,)
            )
        }
    finally:
        conn.close()


def test_explorer_sorts_and_marks(client: TestClient, scanned: int) -> None:
    ids = entry_ids(client, scanned)
    by_name = client.get(f"/scans/{scanned}/space", params={"sort": "name"})
    assert by_name.status_code == 200
    assert_safe_html(by_name.text)
    shown = re.findall(r'data-basket-item value="(\d+)"', by_name.text)
    names = {v: k for k, v in ids.items()}
    order = [names[int(n)] for n in shown]
    # Folders first, then files, each by name.
    assert order[:3] == [XSS_DIR.encode(), b"app", b"photos"]
    assert set(order[3:]) == {b"=SUM(1).txt", b"link-to-tmp"}
    assert ids[b""] not in [int(n) for n in shown]  # the analysed folder itself: no checkbox
    assert client.get(f"/scans/{scanned}/space", params={"sort": "evil"}).status_code == 422

    app = client.get(f"/scans/{scanned}/space", params={"dir": ids[b"app"]})
    assert "🧹 Dépendances Node.js" in app.text

    photos = client.get(f"/scans/{scanned}/space", params={"dir": ids[b"photos"]})
    assert "copies" not in photos.text and "doublons pas encore cherchés" in photos.text
    run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    photos = client.get(f"/scans/{scanned}/space", params={"dir": ids[b"photos"]})
    assert photos.text.count("📑 3 copies") == 3
    assert_safe_html(photos.text)


def test_explorer_pages(client: TestClient, scanned: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explorer, "PAGE_SIZE", 2)
    page = client.get(f"/scans/{scanned}/space", params={"page": 2})
    assert "Page 2 sur 3" in page.text
    assert len(re.findall(r"data-basket-item", page.text)) == 2
    last = client.get(f"/scans/{scanned}/space", params={"page": 99})
    assert "Page 3 sur 3" in last.text


def test_entry_page(client: TestClient, scanned: int) -> None:
    run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    ids = entry_ids(client, scanned)
    page = client.get(f"/scans/{scanned}/entries/{ids[b'photos/a.jpg']}")
    assert page.status_code == 200
    assert_safe_html(page.text)
    assert "Fichier en 3 exemplaires" in page.text
    assert "photos/&lt;img src=x onerror=alert(1)&gt;.jpg" in page.text
    assert f'data-basket-item value="{ids[b"photos/a.jpg"]}"' in page.text

    link = client.get(f"/scans/{scanned}/entries/{ids[b'link-to-tmp']}")
    assert "Pointe vers" in link.text and "non suivi" in link.text
    protected = client.get(f"/scans/{scanned}/entries/{ids[b'app/node_modules/dep/index.js']}")
    assert "🛡️ Protégé" in protected.text
    root = client.get(f"/scans/{scanned}/entries/{ids[b'']}")
    assert root.status_code == 200 and "data-basket-item" not in root.text
    assert client.get(f"/scans/{scanned}/entries/999999").status_code == 404
    assert client.get(f"/scans/999/entries/{ids[b'']}").status_code == 404


def test_basket_content_and_command(client: TestClient, scanned: int) -> None:
    ids = entry_ids(client, scanned)
    assert_safe_html(client.get(f"/scans/{scanned}/basket").text)
    chosen = [
        ids[b"photos"],
        ids[b"photos/a.jpg"],  # inside a selected folder
        ids[XSS_DIR.encode()],
        ids[b"app/node_modules/dep/index.js"],  # protected
        ids[b""],  # the analysed folder itself
        999999,
    ]
    url = f"/api/scans/{scanned}/basket"
    assert client.post(url, json={"entries": chosen}).status_code == 403
    content = client.post(url, json={"entries": chosen}, headers=CSRF).json()
    assert [i["path"] for i in content["items"]] == [
        XSS_DIR,
        "app/node_modules/dep/index.js",
        "photos",
    ]
    assert content["covered"] == [ids[b"photos/a.jpg"]]
    assert content["unknown"] == [999999]
    assert content["root_refused"] is True
    assert content["items"][1]["protection"].startswith("logiciel ou dépendances")

    command = client.post(f"{url}/command", json={"entries": chosen}, headers=CSRF)
    assert command.status_code == 200
    data = command.json()
    assert data["command"].startswith("gio trash -- \\\n  ")
    assert f"'{DISPLAY}/\"><svg onload=alert(2)>'" in data["command"]
    assert f"'{DISPLAY}/photos'" in data["command"]
    assert "a.jpg" not in data["command"]
    assert f"'{DISPLAY}'" not in data["command"]  # never the whole disk
    assert any("dossier analysé entier" in w for w in data["warnings"])
    assert any(w.startswith("Protégé") for w in data["warnings"])

    archive = client.post(
        f"{url}/command", json={"entries": [ids[b"photos"]], "action": "archive"}, headers=CSRF
    ).json()
    assert "tar --create" in archive["command"]
    assert archive["command"].endswith(" -- \\\n  ./photos")
    only_root = client.post(f"{url}/command", json={"entries": [ids[b""]]}, headers=CSRF)
    assert only_root.status_code == 422
    assert client.post(url, json={"entries": []}, headers=CSRF).status_code == 422
    assert (
        client.post("/api/scans/999/basket", json={"entries": [1]}, headers=CSRF).status_code == 404
    )


# --- this computer: personal folder and internal partitions (D-037) ----------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    base = tmp_path / "home"
    touch(base / ".ssh" / "id_ed25519", b"secret key")
    touch(base / ".config" / "google-chrome" / "Cookies", b"cookies")
    touch(base / ".config" / "app.conf", b"ok")
    touch(base / "Documents" / "cv.pdf", b"cv")
    return base


@pytest.fixture
def computer_client(
    tmp_path: Path, home: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> Iterator[TestClient]:
    from treeaudit.roots.external import disk_root_id
    from treeaudit.roots.personal import personal_root
    from treeaudit.roots.registry import RootStatus

    internal = RootConfig(
        id=disk_root_id("/mnt/Data"),
        path=str(root),
        label="Data",
        display_path="/mnt/Data",
        external=True,
        internal=True,
    )
    personal = personal_root(str(home), display_path="/home/me")
    # TreeAudit's index lives inside the personal folder here.
    config = AppConfig(
        data_dir=str(home / ".local" / "share" / "treeaudit"), roots=(personal, internal)
    )
    statuses = (RootStatus(root=personal), RootStatus(root=internal))
    mounts = data_disk_table(str(tmp_path))
    app = create_app(
        config, statuses, mounts=lambda: mounts, home=lambda: None, external_enabled=True
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client
    app.state.jobs.cancel()
    app.state.jobs.wait()


def test_this_computer_is_offered(computer_client: TestClient) -> None:
    page = computer_client.get("/scan/new")
    assert_safe_html(page.text)
    assert "Sur cet ordinateur" in page.text
    assert "🏠 Mon dossier personnel" in page.text and "/home/me" in page.text
    assert "💽 Data" in page.text and "Partition de données du disque système" in page.text
    assert "branché <strong>et ouvert</strong>" in page.text


def test_picker_hides_private_and_own_folders(computer_client: TestClient, home: Path) -> None:
    (home / ".local" / "share" / "treeaudit").mkdir(parents=True, exist_ok=True)
    top = computer_client.get("/api/roots/home/folders").json()["folders"]
    assert "Documents" in top and ".config" in top
    assert ".ssh" not in top
    config = computer_client.get("/api/roots/home/folders", params={"path": ".config"}).json()
    assert config["folders"] == []  # google-chrome is private
    share = computer_client.get("/api/roots/home/folders", params={"path": ".local/share"}).json()
    assert share["folders"] == []  # TreeAudit's own index
    page = computer_client.get("/scan/new", params={"root": "home"})
    assert ".ssh" not in page.text


def test_personal_folder_scan_marks_what_holds_hidden_folders(
    computer_client: TestClient,
) -> None:
    snapshot = run_job(computer_client, "/api/scans", {"root_id": "home", "subpath": ""})
    scan_id = int(snapshot["scan_id"])  # type: ignore[call-overload]
    ids = entry_ids(computer_client, scan_id)
    assert b".ssh" not in ids and b".ssh/id_ed25519" not in ids
    assert b".config/google-chrome" not in ids and b".config/app.conf" in ids
    assert not any(path.startswith(b".local/share/treeaudit") for path in ids)

    summary = computer_client.get(f"/scans/{scan_id}")
    assert "dossier privé, jamais lu" in summary.text
    assert "dossier de TreeAudit" in summary.text
    space = computer_client.get(f"/scans/{scan_id}/space")
    assert_safe_html(space.text)
    assert space.text.count("🛡️ protégé") >= 2  # .config and .local
    command = computer_client.post(
        f"/api/scans/{scan_id}/basket/command",
        json={"entries": [ids[b".config"], ids[b".local"], ids[b"Documents"]]},
        headers=CSRF,
    ).json()
    warnings = " ".join(command["warnings"])
    assert "contient des dossiers privés" in warnings
    assert "contient TreeAudit lui-même" in warnings
    assert command["command"].endswith("\\\n  /home/me/Documents")


# --- quitting (native mode, D-038) --------------------------------------------------------


def test_quit_is_only_offered_natively(client: TestClient) -> None:
    assert "data-quit" not in client.get("/").text
    assert client.post("/api/quit", json={}, headers=CSRF).status_code == 404


def test_quit_stops_the_server_and_cancels_the_job(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(root), label="Disque"),),
    )
    mounts = data_disk_table(str(tmp_path))
    statuses = evaluate_roots(config, mounts, home=None)
    stopped: list[bool] = []
    app = create_app(
        config,
        statuses,
        mounts=lambda: mounts,
        home=lambda: None,
        quit=lambda: stopped.append(True),
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as quitting:
        page = quitting.get("/")
        assert "data-quit" in page.text and "⏻ Quitter" in page.text
        assert quitting.post("/api/quit", json={}).status_code == 403  # CSRF header required
        assert stopped == []
        assert quitting.post("/api/quit", json={}, headers=CSRF).json() == {"stopping": True}
        assert stopped == [True]


# --- disks seen live in the native mode (D-038) --------------------------------------------


def test_disks_plugged_in_while_running_are_offered(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    from treeaudit.roots.external import ExternalDisk, disk_root_id
    from treeaudit.roots.registry import RootStatus

    mounts = data_disk_table(str(tmp_path))
    base = AppConfig(data_dir=str(tmp_path / "data"), roots=())
    plugged: list[bool] = []
    usb_id = disk_root_id("/run/media/me/USB")

    def detect() -> tuple[AppConfig, tuple[RootStatus, ...], list[ExternalDisk]]:
        if not plugged:
            return base, (), []
        usb = RootConfig(
            id=usb_id, path=str(root), label="USB", display_path="/run/media/me/USB", external=True
        )
        config = AppConfig(data_dir=base.data_dir, roots=(usb,))
        return (
            config,
            (RootStatus(root=usb),),
            [ExternalDisk("USB", "/run/media/me/USB", usb_id, "exfat")],
        )

    app = create_app(
        base, (), mounts=lambda: mounts, home=lambda: None, external_enabled=True, detect=detect
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as live:
        before = live.get("/scan/new").text
        assert "🔄 Actualiser" in before and "🔌 USB" not in before
        assert live.post("/api/scans", json={"root_id": usb_id}, headers=CSRF).status_code == 404

        plugged.append(True)  # the disk is plugged in and opened
        after = live.get("/scan/new").text
        assert "🔌 USB" in after
        snapshot = run_job(live, "/api/scans", {"root_id": usb_id, "subpath": ""})
        assert snapshot["status"] == "complete"

        plugged.clear()  # removed: no longer offered
        assert "🔌 USB" not in live.get("/scan/new").text
        assert live.get("/").status_code == 200  # earlier analyses stay readable
        app.state.jobs.wait()


def test_docker_mode_says_to_restart(client: TestClient) -> None:
    page = client.get("/scan/new").text
    assert "🔄 Actualiser" not in page


# --- tidying (D-041) ------------------------------------------------------------------------


def test_organize_preview_and_scripts(client: TestClient, scanned: int) -> None:
    import shlex

    page = client.get(f"/scans/{scanned}/organize")
    assert page.status_code == 200
    assert_safe_html(page.text)
    assert "Rien n'est modifié" in page.text and "Rangé" in page.text
    assert "Photos" in page.text and "Projets" in page.text  # photos/, and app/ (package.json)
    assert "&lt;svg onload=alert(2)&gt;" in page.text  # a hostile project name, escaped

    script = client.get(f"/export/scans/{scanned}/organize.sh")
    assert script.status_code == 200
    assert script.headers["content-disposition"].startswith("attachment;")
    assert script.headers["content-type"].startswith("text/x-shellscript")
    text = script.text
    assert f"cd -- {shlex.quote(DISPLAY)} || exit 1" in text
    # Hostile names reach the script as single, quoted words.
    moves = [
        shlex.split(line) for line in text.splitlines() if line.startswith(("file ", "folder "))
    ]
    assert ["folder", "app", "Rangé/Projets/app"] in moves
    assert ["folder", XSS_DIR, f"Rangé/Projets/{XSS_DIR}"] in moves
    assert any(w[:2] == ["folder", "photos"] and w[2].startswith("Rangé/Photos/") for w in moves)
    assert "rm " not in text and "rm -" not in text

    undo = client.get(f"/export/scans/{scanned}/organize-undo.sh")
    assert undo.status_code == 200 and "back " in undo.text
    assert "rmdir --" in undo.text and "rm -" not in undo.text

    assert client.get(f"/scans/{scanned}/organize?dir=999999").status_code == 404
    assert client.get(f"/export/scans/{scanned}/organize.sh?dir=999999").status_code == 404
    space = client.get(f"/scans/{scanned}/space").text
    assert "🗂️ Ranger ce dossier" in space


def test_organize_adjustments_reach_the_scripts(client: TestClient, scanned: int) -> None:
    ids = entry_ids(client, scanned)
    query = {
        "exclure": str(ids[b"app"]),
        "nom_Photos": "Images",
        "destination": "Classement",
    }
    page = client.get(f"/scans/{scanned}/organize", params=query)
    assert page.status_code == 200
    assert_safe_html(page.text)
    assert "Laissés en place à ta demande (1)" in page.text
    assert "Classement/Images" in page.text
    script = client.get(f"/export/scans/{scanned}/organize.sh", params=query).text
    assert " Classement/Images/" in script and "Rangé" not in script
    assert "folder app " not in script
    bad = client.get(f"/export/scans/{scanned}/organize.sh", params={"nom_Photos": "../../etc"})
    assert bad.status_code == 422
    refused = client.get(f"/scans/{scanned}/organize", params={"destination": ".cache"})
    assert "Ajustement refusé" in refused.text


def test_ai_suggestions_are_shown_never_applied(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    from typing import Any

    from treeaudit.llm.client import Answer, LlmClient

    class Organizer(LlmClient):
        def __init__(self) -> None:
            super().__init__("http://llm:8080")

        def ask(self, system: str, user: str, schema: Any, *, max_tokens: int = 400) -> Answer:
            if "sous_dossiers" in schema["properties"]:
                data: dict[str, Any] = {"sous_dossiers": ["Factures", "../../etc", XSS]}
            else:
                keys = schema["properties"]["classement"]["items"]["properties"]["id"]["enum"]
                data = {"classement": [{"id": key, "sous_dossier": "Factures"} for key in keys]}
            return Answer(data=data, seconds=0.1, tokens=1, tokens_per_second=1.0)

    touch(root / "papiers" / "a.pdf", b"a")
    touch(root / "papiers" / "b.txt", b"b")
    touch(root / "papiers" / "c.mp3", b"c")  # a mixed folder: its files are proposed one by one
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(root), label="Disque", display_path=DISPLAY),),
    )
    mounts = data_disk_table(str(tmp_path))
    statuses = evaluate_roots(config, mounts, home=None)
    app = create_app(config, statuses, mounts=lambda: mounts, home=lambda: None, llm=Organizer())
    with TestClient(app, base_url="http://127.0.0.1:8765") as expert:
        snapshot = run_job(expert, "/api/scans", {"root_id": "main", "subpath": ""})
        scan_id = snapshot["scan_id"]
        plain = expert.get(f"/scans/{scan_id}/organize").text
        assert "💡 Suggestions de l'IA" in plain and "ai-panel" not in plain
        url = f"/api/scans/{scan_id}/organize/suggestions"
        body = {"category": "Documents", "query": ""}
        assert expert.post(url, json=body).status_code == 403  # CSRF header required
        bad = expert.post(url, json={"category": "../../etc"}, headers=CSRF)
        assert bad.status_code == 422
        job = run_job(expert, url, body)
        assert job["kind"] == "suggest" and job["status"] == "complete"
        assert job["result_url"].startswith(f"/scans/{scan_id}/organize?suggestions=")
        page = expert.get(job["result_url"]).text
        assert_safe_html(page)
        assert "rien ne change tant que tu ne les acceptes pas" in page
        assert 'data-suggestion="Factures"' in page
        assert "../../etc" not in page and "onerror" not in page.split("ai-panel")[1]
        # Shown only: the plan itself is unchanged until the user submits.
        assert 'name="sous_' in page and 'value="Factures"' not in page
        script = expert.get(f"/export/scans/{scan_id}/organize.sh").text
        assert "/Factures/" not in script
        expert.app.state.jobs.wait()  # type: ignore[attr-defined]


def test_no_ai_without_expert_mode(client: TestClient, scanned: int) -> None:
    page = client.get(f"/scans/{scanned}/organize").text
    assert "💡 Suggestions de l'IA" not in page and "ai-panel" not in page
    started = client.post(
        f"/api/scans/{scanned}/organize/suggestions",
        json={"category": "Documents"},
        headers=CSRF,
    )
    assert started.status_code == 409 and "mode expert" in started.json()["detail"]


# --- opinions of the local AI on the folders to check (D-040) ------------------------------


def test_ai_opinions_are_shown_never_change_the_selection(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    from typing import Any

    from treeaudit.llm.client import Answer, LlmClient

    seen: list[str] = []

    class Reviewer(LlmClient):
        def __init__(self) -> None:
            super().__init__("http://llm:8080")

        def ask(self, system: str, user: str, schema: Any, *, max_tokens: int = 400) -> Answer:
            seen.append(user)
            personal = "IMG_" in user
            data = {
                "contenu_attendu": not personal,
                "fichiers_personnels": personal,
                "avis": "nettoyable",  # always: the guard and the display must cope
                "confiance": 90,
                "raisons": ["des photos" if personal else "rien de personnel"],
            }
            return Answer(data=data, seconds=0.1, tokens=1, tokens_per_second=1.0)

    touch(root / "Images" / "venv" / "IMG_0001.jpg", b"photo")
    touch(root / "dev" / "build" / "out.o", b"o")
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(root), label="Disque", display_path=DISPLAY),),
    )
    mounts = data_disk_table(str(tmp_path))
    statuses = evaluate_roots(config, mounts, home=None)
    app = create_app(config, statuses, mounts=lambda: mounts, home=lambda: None, llm=Reviewer())
    with TestClient(app, base_url="http://127.0.0.1:8765") as expert:
        snapshot = run_job(expert, "/api/scans", {"root_id": "main", "subpath": ""})
        scan_id = snapshot["scan_id"]
        before = expert.get(f"/scans/{scan_id}/parasites", params={"min_confidence": 0}).text
        assert "💡 Avis de l'IA" in html.unescape(before)
        checked_before = before.count(" checked")
        url = f"/api/scans/{scan_id}/parasites/opinions"
        assert expert.post(url, json={}).status_code == 403  # CSRF header required
        job = run_job(expert, url, {"min_confidence": 0})
        assert job["kind"] == "opinions" and job["result_url"].endswith(f"avis={job['id']}")
        page = expert.get(job["result_url"]).text
        assert_safe_html(page)
        page = html.unescape(page)
        # The guard turned "nettoyable" into "keep" for the photos; never a recommendation.
        assert "L'IA conseille de garder ce dossier" in page
        assert "avis corrigé par le garde-fou de TreeAudit" in page
        assert "ce n'est pas une recommandation" in page
        assert page.count(" checked") == checked_before  # nothing checked nor unchecked
        # Metadata only: names and extensions, never a file's content.
        assert any('"IMG_0001.jpg"' in prompt for prompt in seen)
        assert not any(
            "photo" in prompt.split("children")[0] for prompt in seen if "IMG_" in prompt
        )
        expert.app.state.jobs.wait()  # type: ignore[attr-defined]


def test_no_opinions_without_expert_mode(client: TestClient, scanned: int) -> None:
    assert "Avis de l" not in client.get(f"/scans/{scanned}/parasites").text
    started = client.post(f"/api/scans/{scanned}/parasites/opinions", json={}, headers=CSRF)
    assert started.status_code == 409


# --- forgetting analyses, compacting the index (D-043) -------------------------------------


def test_forgetting_an_analysis_erases_only_treeaudits_records(
    client: TestClient, scanned: int, root: Path
) -> None:
    def files_on_disk() -> dict[str, bytes]:
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and not p.is_symlink()
        }

    before = files_on_disk()
    run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    client.get(f"/scans/{scanned}")  # caches the summaries
    other = run_job(client, "/api/scans", {"root_id": "main", "subpath": "photos"})["scan_id"]

    home = client.get("/").text
    assert_safe_html(home)
    assert f"{DISPLAY}/photos" in home and DISPLAY in home  # where each analysis comes from
    assert "Index de TreeAudit" in home

    url = f"/api/scans/{scanned}/forget"
    assert client.post(url, json={}).status_code == 403  # CSRF header required
    assert client.post(url, json={}, headers=CSRF).json() == {"forgotten": True}
    assert client.post(url, json={}, headers=CSRF).status_code == 404  # already gone

    data_dir = client.app.state.config.data_dir  # type: ignore[attr-defined]
    conn = sqlite3.connect(database_path(data_dir))
    try:
        count = lambda sql: conn.execute(sql, (scanned,)).fetchone()[0]  # noqa: E731
        assert count("SELECT COUNT(*) FROM entry WHERE scan_id = ?") == 0
        assert count("SELECT COUNT(*) FROM dup_run WHERE scan_id = ?") == 0
        assert conn.execute("SELECT COUNT(*) FROM file_hash").fetchone()[0] == 0
        assert count("SELECT COUNT(*) FROM summary_cache WHERE key LIKE '%:' || ? || ':%'") == 0
        kept = conn.execute("SELECT COUNT(*) FROM entry WHERE scan_id = ?", (other,)).fetchone()[0]
        assert kept > 0  # the other analysis is untouched
    finally:
        conn.close()
    assert files_on_disk() == before  # never a file of the analysed folder
    assert client.get(f"/scans/{scanned}").status_code == 404


def test_forgetting_waits_for_the_running_job(
    client: TestClient, scanned: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Running:
        running = True

    monkeypatch.setattr(client.app.state.jobs, "current", Running())  # type: ignore[attr-defined]
    refused = client.post(f"/api/scans/{scanned}/forget", json={}, headers=CSRF)
    assert refused.status_code == 409


def test_compacting_the_index(client: TestClient, scanned: int) -> None:
    client.post(f"/api/scans/{scanned}/forget", json={}, headers=CSRF)
    job = run_job(client, "/api/index/compact", {})
    assert job["kind"] == "compact" and job["status"] == "complete"
    assert job["result_url"] == f"/?compacte={job['id']}"
    assert "Index compacté" in client.get(job["result_url"]).text


def test_projects_page(client: TestClient, scanned: int) -> None:
    page = client.get(f"/scans/{scanned}/projects")
    assert page.status_code == 200
    assert_safe_html(page.text)
    assert "📦 Projets et logiciels" in page.text and "Rien n'est modifié" in page.text
    assert "JavaScript / TypeScript" in page.text
    assert "&lt;svg onload=alert(2)&gt;" in page.text  # a hostile project name, escaped
    # Its node_modules is offered checked, with the cleanup page's number.
    numbers = re.findall(r'data-pick="(\d+)" data-band="green"\s+checked', page.text)
    assert numbers
    url = f"/api/scans/{scanned}/parasites/command"
    command = client.post(
        url, json={"numbers": [int(n) for n in numbers], "action": "trash"}, headers=CSRF
    ).json()["command"]
    assert f"'{DISPLAY}/app/node_modules'" in command
    oldest = client.get(f"/scans/{scanned}/projects?tri=date&voir=logiciel")
    assert oldest.status_code == 200 and "Aucun projet ni logiciel" in oldest.text
    assert client.get(f"/scans/{scanned}/projects?tri=rm").status_code == 422
    assert f'href="/scans/{scanned}/projects"' in client.get(f"/scans/{scanned}").text
