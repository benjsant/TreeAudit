"""Web interface: pages, JSON API, jobs, commands and exports, with hostile names."""

from __future__ import annotations

import base64
import csv
import errno
import io
import os
import re
import sqlite3
import threading
import zlib
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from treeaudit.archive import destination
from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.db import images as images_db
from treeaudit.db.connection import connect, database_path
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.registry import evaluate_roots
from treeaudit.roots.validation import RejectReason
from treeaudit.web import explorer, pages
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
        if tag == "img":  # only the previews TreeAudit serves itself (D-046)
            src = dict(attrs).get("src") or ""
            if not re.fullmatch(r"/api/scans/\d+/entries/\d+/image", src):
                self.problems.append(f"unexpected <img src={src!r}>")
        if tag in ("svg", "iframe", "object", "embed"):
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


def test_compacting_is_offered_only_when_it_can_give_space_back(
    client: TestClient, root: Path
) -> None:
    assert "data-compact-index" not in client.get("/").text  # fresh install: nothing to do
    scan_id = run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})["scan_id"]
    assert "data-compact-index" in client.get("/").text
    for i in range(300):  # enough rows for the forgotten analysis to leave free pages
        touch(root / "many" / f"{i:04d}-{'x' * 120}.txt", b"x")
    big = run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})["scan_id"]
    for forgotten in (scan_id, big):
        assert (
            client.post(f"/api/scans/{forgotten}/forget", json={}, headers=CSRF).status_code == 200
        )
    # No analysis left, but their place is still in the index: compacting gives it back.
    assert "data-compact-index" in client.get("/").text


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
    # Found on the similar images page: .button's display showed hidden buttons.
    assert "[hidden] { display: none !important; }" in css


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


def test_a_job_keeps_the_roots_it_was_started_with(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roots refreshed while a job waits to run (a disk plugged in) do not
    change the configuration that job validates against."""
    seen: list[AppConfig] = []
    pending: list[Callable[[jobs_module.Job], object]] = []

    def recording_scan(config: AppConfig, *args: object, **kwargs: object) -> None:
        seen.append(config)
        raise jobs_module.JobError("stopped by the test")

    manager = client.app.state.jobs  # type: ignore[attr-defined]
    monkeypatch.setattr(jobs_module, "execute_scan", recording_scan)
    # The work is kept instead of run in a thread: it runs after the refresh, for sure.
    monkeypatch.setattr(manager, "_start", lambda job, work: pending.append(work) or job)
    before = manager.config
    job = manager.start_scan("main", "")
    manager.config = AppConfig(data_dir=before.data_dir, roots=())  # refreshed meanwhile
    with pytest.raises(jobs_module.JobError):
        pending[0](job)
    assert seen == [before]


def test_scan_page_and_explorer(client: TestClient, scanned: int) -> None:
    page = client.get(f"/scans/{scanned}")
    assert page.status_code == 200
    assert "photos" in page.text
    assert_safe_html(page.text)
    assert "Où va la place" in page.text and "À nettoyer" in page.text
    # Why a scan is quick: it never reads what is in the files.
    assert "jamais son contenu : c'est pour cela qu'elle est rapide" in page.text
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
    elapsed = re.search(r'data-field="elapsed">([^<]*)<', page.text)
    assert elapsed and re.fullmatch(r"\d+,\d", elapsed.group(1)), elapsed


def test_the_script_writes_decimals_with_a_comma(client: TestClient) -> None:
    script = client.get("/static/app.js").text
    # One place rounds a decimal, and it writes the French comma.
    assert script.count("toFixed(") == 1
    assert 'toFixed(1).replace(".", ",")' in script
    assert "String(job.elapsed)" not in script


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
    # The page shows the last stage's own counters, not files read once.
    progress = snapshot["progress"]
    assert progress["stage"] == "lecture complète"
    # Small files: their head was the whole file, nothing was left to read.
    assert progress["stage_done"] == progress["stage_total"]
    assert progress["stage_bytes_done"] == progress["stage_bytes_total"]
    script = client.get("/static/app.js").text
    assert "fichiers comparés" not in script and "lus sur" in script
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


def test_invisible_characters_of_a_name_are_shown(client: TestClient, root: Path) -> None:
    """A newline or a tab in a name used to show as a space, and a direction
    mark reversed the end of the name: each is now written as an escape."""
    for name in ("nouvelle\nligne.txt", "tab\tulation.txt", "photo\u202egpj.exe"):
        touch(root / "noms" / name, b"x")
    scan_id = int(run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})["scan_id"])  # type: ignore[call-overload]
    ids = entry_ids(client, scan_id)
    folder = client.get(f"/scans/{scan_id}/space?dir={ids[b'noms']}").text
    entry = client.get(f"/scans/{scan_id}/entries/{ids[b'noms/nouvelle\nligne.txt']}").text
    for page in (folder, entry):
        assert "nouvelle\\x0aligne.txt" in page
        assert "nouvelle\nligne" not in page and "nouvelle ligne" not in page
    assert "tab\\x09ulation.txt" in folder and "photo\\u202egpj.exe" in folder
    assert "\u202e" not in folder
    # The picker never offers such a folder, nor takes it from a hand-made link:
    # its path is written into the page, escaped, and sent back from there.
    assert client.get("/scan/new?root=main&path=noms%0Aautre").status_code == 422
    assert client.get("/scan/new?root=main&path=noms").status_code == 200
    # Names the script shows (basket, progress, archive) go through its visible().
    script = client.get("/static/app.js").text
    for shown in (
        "visible(item.path)",
        "visible(p.current_dir)",
        "visible(problem.path)",
        "nom non affichable sans risque) : ${visible(path)}",
        "⚠ ${visible(warning)}",
    ):
        assert shown in script


def test_the_duplicates_page_points_to_similar_folders(client: TestClient, scanned: int) -> None:
    """A copied folder shows as many file groups: the page says where whole
    folders are compared, as the summary does."""
    run = run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    page = client.get(f"/dupes/{run['run_id']}").text
    assert f'href="/scans/{scanned}/similar">🪞 voir les dossiers presque identiques' in page


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_unread_files_are_listed_under_the_warning(
    client: TestClient, root: Path, scanned: int
) -> None:
    """An incomplete search says how many files it could not read, which, and
    why in words, right under its warning (not only in the technical details)."""
    locked = root / "photos" / "a.jpg"
    locked.chmod(0o000)
    try:
        snapshot = run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    finally:
        locked.chmod(0o644)
    assert snapshot["status"] == "incomplete"
    page = client.get(f"/dupes/{snapshot['run_id']}").text
    assert_safe_html(page)
    warning = page.split('<div class="notice warn" role="alert">')[1].split("</div>")[0]
    assert "La recherche n'a pas pu lire 1 fichier(s)" in warning
    assert "photos/a.jpg" in warning and "accès refusé" in warning
    # The system's words stay in a tooltip only.
    assert '<span title="open : Permission denied">accès refusé</span>' in warning


@pytest.mark.parametrize(
    ("operation", "code", "said"),
    [
        ("read", errno.EIO, "erreur de lecture : disque débranché ou abîmé ?"),
        ("open", errno.ENOTCONN, "disque débranché"),
        (
            "changed",
            None,
            "a changé depuis l'analyse (ou le disque a été débranché) : relance l'analyse",
        ),
        ("read", errno.EOVERFLOW, "Value too large"),  # unusual: the system's words
    ],
)
def test_errors_are_said_in_words(operation: str, code: int | None, said: str) -> None:
    error = {"operation": operation, "errno": code, "message": "Value too large"}
    assert pages._reason(error) == said


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
    # Choosing the personal folder does not mean analysing all of it.
    assert "En entier, ou seulement un de ses dossiers" in page.text
    inside = computer_client.get("/scan/new", params={"root": "home"})
    assert "ouvre un de ses dossiers plus bas pour n'analyser que lui" in inside.text


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
        listed = [root["id"] for root in live.get("/api/roots").json()["roots"]]
        assert listed == [usb_id]  # the JSON list sees the disk too, not only the page
        snapshot = run_job(live, "/api/scans", {"root_id": usb_id, "subpath": ""})
        assert snapshot["status"] == "complete"

        plugged.clear()  # removed: no longer offered
        assert "🔌 USB" not in live.get("/scan/new").text
        assert live.get(f"/scan/new?root={usb_id}").status_code == 404
        assert live.get("/api/roots").json()["roots"] == []
        assert live.get("/").status_code == 200  # earlier analyses stay readable

        # Plugged in again: a link to it still open works at once, without
        # going through the list of disks first (seen with a real USB key).
        plugged.append(True)
        assert live.get(f"/scan/new?root={usb_id}").status_code == 200
        snapshot = run_job(live, "/api/scans", {"root_id": usb_id, "subpath": ""})
        assert snapshot["status"] == "complete"
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


def test_unfolded_categories_stay_unfolded(client: TestClient, scanned: int) -> None:
    import re

    def unfolded(params: dict[str, str | list[str]]) -> set[str]:
        page = client.get(f"/scans/{scanned}/organize", params=params)
        assert page.status_code == 200
        found = re.findall(r'<input type="hidden" name="ouvert" value="([^"]+)">', page.text)
        opened = re.findall(r'<details class="group-band" data-unfold open>', page.text)
        assert len(found) == len(opened)
        return set(found)

    first = unfolded({})
    assert len(first) == 1  # the first category, as before
    assert unfolded({"ouvert": "Projets"}) == {"Projets"}
    assert unfolded({"ouvert": ["Projets", "Photos"]}) == {"Projets", "Photos"}
    assert unfolded({"ouvert": "Projets", "proposer": "Photos"}) == {"Projets", "Photos"}
    # An unknown name is ignored, and never echoed back.
    assert unfolded({"ouvert": "<b>x</b>"}) == first


def test_organize_marks_copies_once_duplicates_are_known(client: TestClient, scanned: int) -> None:
    photos = entry_ids(client, scanned)[b"photos"]
    url = f"/scans/{scanned}/organize?dir={photos}"
    before = client.get(url).text
    assert "📑 en double" not in before  # not searched yet: nothing claimed
    run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    after = client.get(url).text
    assert_safe_html(after)
    assert after.count("📑 en double") == 3
    assert "depuis <span" not in after  # loose files of the folder itself: the name says it all
    assert 'title="3 fichiers identiques : voir où sont les autres"' in after
    assert re.search(rf'href="/scans/{scanned}/entries/\d+" title="3 fichiers', after)
    # Only shown: the script is the same.
    assert client.get(f"/export/scans/{scanned}/organize.sh?dir={photos}").status_code == 200


def test_organize_lists_lines_by_sub_folder(client: TestClient, scanned: int) -> None:
    ids = entry_ids(client, scanned)
    photos = ids[b"photos"]
    names = [b"photos/a.jpg", f"photos/{XSS}.jpg".encode(), f"photos/{FORMULA}.jpg".encode()]
    last = ids[names[-1]]
    page = client.get(
        f"/scans/{scanned}/organize", params={"dir": str(photos), f"sous_{last}": "Zeta"}
    ).text
    order = [int(n) for n in re.findall(r'name="sous_(\d+)"', page)]
    assert order[0] == last  # the one with a sub-folder comes first
    assert sorted(order) == sorted(ids[name] for name in names)  # nothing lost


def test_rule_sub_folders_are_shown_never_applied(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    for name in ("facture-edf.pdf", "Facture SFR.pdf", "avis_imposition.pdf", "Avis d'impôt.pdf"):
        touch(root / "papiers" / name, name.encode())
    touch(root / "papiers" / f"{XSS} facture.pdf", b"x")
    touch(root / "papiers" / "notes.txt", b"n")
    touch(root / "papiers" / "c.mp3", b"c")  # a mixed folder: its files are proposed one by one
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(root), label="Disque", display_path=DISPLAY),),
    )
    mounts = data_disk_table(str(tmp_path))
    app = create_app(
        config, evaluate_roots(config, mounts, home=None), mounts=lambda: mounts, home=lambda: None
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as plain:
        scan_id = run_job(plain, "/api/scans", {"root_id": "main", "subpath": ""})["scan_id"]
        page = plain.get(f"/scans/{scan_id}/organize").text
        # For everyone: no expert mode needed (D-050).
        assert "📂 Proposer des sous-dossiers" in page and "💡" not in page
        assert "proposer=Documents" in page and "ai-panel" not in page
        assert "proposer=Musique" not in page  # one element: nothing could be proposed
        assert 'depuis <span class="path">papiers/' in page  # from a sub-folder: said so
        page = plain.get(f"/scans/{scan_id}/organize", params={"proposer": "Documents"}).text
        # The reason, in the tooltip and written under the element (no hover on phones).
        assert 'title="le nom contient « facture ». Clique pour l\'accepter."' in page
        assert "· 📂 le nom contient « facture »" in page
        assert_safe_html(page)
        assert "📂 Sous-dossiers proposés" in page and "règles fixes" in page
        assert page.count('data-suggestion="Factures"') == 3
        assert page.count('data-suggestion="Impôts"') == 2
        # Lines of one proposed sub-folder are next to each other, in the order proposed.
        assert re.findall(r'data-suggestion="([^"]+)"', page) == ["Factures"] * 3 + ["Impôts"] * 2
        assert "le nom contient « facture »" in page
        assert "onerror" not in page.split("ai-panel")[1].split("</section>")[0]
        # Shown only: the plan and its script are unchanged until the user submits.
        assert 'value="Factures"' not in page
        script = plain.get(
            f"/export/scans/{scan_id}/organize.sh", params={"proposer": "Documents"}
        ).text
        assert "/Factures/" not in script
        for odd in ("Projets", "../../etc", "x" * 41):
            other = plain.get(f"/scans/{scan_id}/organize", params={"proposer": odd})
            assert other.status_code in (200, 422) and "ai-panel" not in other.text


def test_the_next_batch_of_sub_folders_after_accepting_the_first(
    tmp_path: Path, root: Path, data_disk_table: Callable[..., MountTable]
) -> None:
    from treeaudit.organize.subfolders import MAX_SUBFOLDERS

    last = "abcdefghijklmnopqrtu"  # never s nor x: plurals are folded together
    words = ["mot" + chr(97 + i // len(last)) + last[i % len(last)] for i in range(32)]
    for word in words:
        for n in "ab":
            touch(root / "papiers" / f"{word} {n}.pdf", b"x")
    touch(root / "papiers" / "c.mp3", b"c")  # a mixed folder: its files are proposed one by one
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        roots=(RootConfig(id="main", path=str(root), label="Disque", display_path=DISPLAY),),
    )
    mounts = data_disk_table(str(tmp_path))
    app = create_app(
        config, evaluate_roots(config, mounts, home=None), mounts=lambda: mounts, home=lambda: None
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as plain:
        scan_id = run_job(plain, "/api/scans", {"root_id": "main", "subpath": ""})["scan_id"]
        url = f"/scans/{scan_id}/organize"
        first = plain.get(url, params={"proposer": "Documents"}).text
        pairs = re.findall(r'data-suggestion="([^"]+)" data-target="(sous_\d+)"', first)
        assert len({name for name, _ in pairs}) == MAX_SUBFOLDERS
        assert "➡ Proposer la suite" in first
        # The user accepts the whole batch and asks for the next one (one form).
        query = [(target, name) for name, target in pairs] + [("proposer", "Documents")]
        second = plain.get(url, params=query).text
        assert_safe_html(second)
        again = re.findall(r'data-suggestion="([^"]+)" data-target="(sous_\d+)"', second)
        assert len({name for name, _ in again}) == 2 and "➡ Proposer la suite" not in second
        assert not {t for _, t in again} & {t for _, t in pairs}  # accepted ones left alone
        for name, target in pairs[:3]:  # and kept in the preview
            assert f'name="{target}" value="{name}"' in second


def test_the_ai_no_longer_proposes_sub_folders(client: TestClient, scanned: int) -> None:
    """Removed in favour of the fixed rules (D-051): no button, no route."""
    page = client.get(f"/scans/{scanned}/organize").text
    assert "💡 Suggestions de l'IA" not in page and "ai-panel" not in page
    started = client.post(
        f"/api/scans/{scanned}/organize/suggestions",
        json={"category": "Documents"},
        headers=CSRF,
    )
    assert started.status_code in (404, 405)


# --- the former expert mode (D-040, removed by D-052) --------------------------------------


def test_no_ai_anywhere(client: TestClient, scanned: int) -> None:
    for url in ("/", f"/scans/{scanned}/parasites", f"/scans/{scanned}/organize"):
        page = client.get(url).text
        assert "💡" not in page and "Mode expert" not in page and "ai-opinion" not in page
    started = client.post(f"/api/scans/{scanned}/parasites/opinions", json={}, headers=CSRF)
    assert started.status_code in (404, 405)


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


def test_the_analysed_folder_is_named_as_the_user_knows_it(
    client: TestClient, scanned: int
) -> None:
    """Not by its mount point in the container (/audit/roots/<id>), found while exploring."""
    for url in (f"/scans/{scanned}/space", f"/scans/{scanned}/organize"):
        crumbs = re.search(r'<nav class="crumbs"[^>]*>(.*?)</nav>', client.get(url).text, re.S)
        assert crumbs is not None
        assert ">My Disk</a>" in crumbs.group(1) and ">root</a>" not in crumbs.group(1)
    projects = client.get(f"/scans/{scanned}/projects").text
    assert re.search(r"recréables · <strong>\d+ coché", projects)


def test_almost_identical_folders_pages(client: TestClient, root: Path) -> None:
    def blob(tag: str) -> bytes:
        return tag.encode().ljust(64, b"_") * 50

    for name in "abc":
        touch(root / "Sauvegarde" / f"{name}.jpg", blob(name))
        touch(root / f"{XSS} copie" / f"{name}.jpg", blob(name))
    touch(root / f"{XSS} copie" / "en plus.jpg", blob("plus"))
    scan_id = run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})["scan_id"]
    url = f"/scans/{scan_id}/similar"

    before = client.get(url)
    assert before.status_code == 200 and "data-start-dupes" in before.text
    assert "/similar" not in client.get(f"/scans/{scan_id}").text  # offered once searched

    run_job(client, f"/api/scans/{scan_id}/dupes", {"micro": False})
    assert f'href="{url}"' in client.get(f"/scans/{scan_id}").text
    page = client.get(url).text
    assert_safe_html(page)
    # The bigger one (one file more) comes first; the other is entirely in it.
    assert "Deux dossiers" in page and "entièrement dans le premier" in page
    assert "&lt;img src=x onerror=alert(1)&gt; copie" in page and XSS not in page
    link = re.search(rf'href="(/scans/{scan_id}/similar/(\d+)/(\d+))"', page)
    assert link is not None

    detail = client.get(link.group(1))
    assert detail.status_code == 200
    assert_safe_html(detail.text)
    assert "✅ Fait : tout ce qui est dans B se trouve aussi dans A." in detail.text
    assert "Seulement dans A</strong> — 1 fichier(s)" in detail.text
    assert XSS not in detail.text

    a_id, b_id = link.group(2), link.group(3)
    assert client.get(f"{url}/{a_id}/{a_id}").status_code == 404  # itself
    assert client.get(f"{url}/999999/{b_id}").status_code == 404
    a_file = entry_ids(client, scan_id)[b"Sauvegarde/a.jpg"]
    assert client.get(f"{url}/{a_file}/{b_id}").status_code == 404  # a file, not a folder
    assert client.get(f"{url}/0/{b_id}").status_code == 422


# --- similar images: signatures computed by the page (D-069) ------------------------------

HASH = "ab" * 32
THUMB = "7f" * 1024


def signed(entry: int, hash_hex: str = HASH) -> dict[str, object]:
    return {"entry": entry, "hash": hash_hex, "thumb": THUMB, "width": 4000, "height": 3000}


def signatures_of(client: TestClient, scan_id: int) -> dict[int, tuple[object, ...]]:
    data_dir = client.app.state.config.data_dir  # type: ignore[attr-defined]
    conn = sqlite3.connect(database_path(data_dir))
    try:
        return {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT entry_id, algo, hash, width, failure FROM image_sig WHERE scan_id = ?",
                (scan_id,),
            )
        }
    finally:
        conn.close()


def test_image_signatures_are_stored_then_reused(client: TestClient, scanned: int) -> None:
    ids = entry_ids(client, scanned)
    url = f"/api/scans/{scanned}/images"
    assert client.post(f"{url}/todo", json={}).status_code == 403  # CSRF guard
    todo = client.post(f"{url}/todo", json={}, headers=CSRF).json()
    images = sorted(ids[p] for p in ids if p.startswith(b"photos/"))
    assert (todo["algo"], todo["total"], todo["done"], todo["failed"]) == (1, 3, 0, 0)
    assert sorted(todo["todo"]) == images
    first, second, broken = todo["todo"]
    body = {
        "algo": 1,
        "items": [signed(first), signed(second), {"entry": broken, "failure": "undecodable"}],
    }
    saved = client.post(f"{url}/signatures", json=body, headers=CSRF)
    assert saved.json() == {"saved": 2, "failed": 1}
    after = client.post(f"{url}/todo", json={}, headers=CSRF).json()
    assert (after["done"], after["failed"], after["todo"]) == (2, 1, [])
    stored = signatures_of(client, scanned)
    assert stored[first] == (1, bytes.fromhex(HASH), 4000, None)
    assert stored[broken] == (1, None, None, "undecodable")

    # Unchanged files: a new analysis reuses the signatures, not the failure
    # (a newer browser may decode that image).
    rescan = run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})
    again = int(rescan["scan_id"])  # type: ignore[call-overload]
    state = client.post(f"/api/scans/{again}/images/todo", json={}, headers=CSRF).json()
    assert (state["total"], state["done"], state["failed"], len(state["todo"])) == (3, 2, 0, 1)

    # Forgetting the analysis forgets its signatures (D-043).
    assert client.post(f"/api/scans/{scanned}/forget", json={}, headers=CSRF).status_code == 200
    assert signatures_of(client, scanned) == {}
    assert len(signatures_of(client, again)) == 2


@pytest.mark.parametrize(
    ("item", "status"),
    [
        ({"entry": 0, "hash": "AB" * 32, "thumb": THUMB, "width": 1, "height": 1}, 422),
        ({"entry": 0, "hash": "ab" * 31, "thumb": THUMB, "width": 1, "height": 1}, 422),
        ({"entry": 0, "hash": HASH, "thumb": THUMB[:-2], "width": 1, "height": 1}, 422),
        ({"entry": 0, "hash": HASH, "width": 1, "height": 1}, 422),
        ({"entry": 0, "hash": HASH, "thumb": THUMB, "width": 0, "height": 1}, 422),
        ({**signed(0), "failure": "undecodable"}, 422),
        ({"entry": 0, "failure": "../../etc/passwd"}, 422),
        ({"entry": -1, "failure": "undecodable"}, 422),
    ],
)
def test_image_signatures_are_checked(
    client: TestClient, scanned: int, item: dict[str, object], status: int
) -> None:
    ids = entry_ids(client, scanned)
    if item["entry"] == 0:
        item = {**item, "entry": ids[b"photos/a.jpg"]}
    body = {"algo": 1, "items": [item]}
    response = client.post(f"/api/scans/{scanned}/images/signatures", json=body, headers=CSRF)
    assert response.status_code == status
    assert signatures_of(client, scanned) == {}


def test_image_signatures_only_for_images_of_the_analysis(client: TestClient, scanned: int) -> None:
    ids = entry_ids(client, scanned)
    url = f"/api/scans/{scanned}/images/signatures"
    image, not_an_image = ids[b"photos/a.jpg"], ids[b"app/package.json"]
    mixed = {"algo": 1, "items": [signed(image), signed(not_an_image)]}
    refused = client.post(url, json=mixed, headers=CSRF)
    assert refused.status_code == 422 and str(not_an_image) in refused.json()["detail"]
    assert signatures_of(client, scanned) == {}  # all or nothing
    old_page = {"algo": 2, "items": [signed(image)]}
    assert client.post(url, json=old_page, headers=CSRF).status_code == 409
    too_many = {"algo": 1, "items": [signed(image)] * 501}
    assert client.post(url, json=too_many, headers=CSRF).status_code == 422
    unknown = {"algo": 1, "items": [signed(image)]}
    assert (
        client.post("/api/scans/999/images/signatures", json=unknown, headers=CSRF).status_code
        == 404
    )
    assert signatures_of(client, scanned) == {}


def test_a_changed_image_is_signed_again(client: TestClient, scanned: int, root: Path) -> None:
    ids = entry_ids(client, scanned)
    images = [ids[b"photos/a.jpg"], ids[f"photos/{XSS}.jpg".encode()]]
    body = {"algo": 1, "items": [signed(i) for i in images]}
    client.post(f"/api/scans/{scanned}/images/signatures", json=body, headers=CSRF)
    (root / "photos" / "a.jpg").write_bytes(b"another picture " * 200)
    rescan = run_job(client, "/api/scans", {"root_id": "main", "subpath": ""})
    again = int(rescan["scan_id"])  # type: ignore[call-overload]
    state = client.post(f"/api/scans/{again}/images/todo", json={}, headers=CSRF).json()
    assert state["done"] == 1
    assert entry_ids(client, again)[b"photos/a.jpg"] in state["todo"]


def test_the_index_refuses_malformed_signatures(client: TestClient, scanned: int) -> None:
    data_dir = client.app.state.config.data_dir  # type: ignore[attr-defined]
    image = entry_ids(client, scanned)[b"photos/a.jpg"]
    limits = {"extensions": {"jpg"}, "max_size": 10**6}
    conn = connect(data_dir)
    try:
        short = images_db.Signature(image, b"\x00" * 31, b"\x00" * 1024, 1, 1)
        with pytest.raises(ValueError, match="wrong size"):
            images_db.save(conn, scanned, [short], [], **limits)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="failure reason"):
            images_db.save(conn, scanned, [], [images_db.Failure(image, "x")], **limits)  # type: ignore[arg-type]
    finally:
        conn.close()
    assert signatures_of(client, scanned) == {}


def test_the_similar_images_page_loads_its_own_script(client: TestClient, scanned: int) -> None:
    summary = client.get(f"/scans/{scanned}")
    assert f'href="/scans/{scanned}/images"' in summary.text  # even before any duplicates search
    page = client.get(f"/scans/{scanned}/images")
    assert page.status_code == 200
    assert_safe_html(page.text)
    assert '<script src="/static/similar_images.js" defer></script>' in page.text
    assert f'data-sign-images data-scan="{scanned}"' in page.text
    assert client.get("/scans/999/images").status_code == 404
    # Only this page loads it: the others do not compute anything.
    assert "similar_images.js" not in summary.text
    script = client.get("/static/similar_images.js").text
    assert "algo: 1," in script and f"algo: {images_db.ALGO}," in script
    assert "innerHTML" not in script


def test_the_self_test_image_of_the_page_has_the_expected_pixels(client: TestClient) -> None:
    """The page refuses a browser whose pixels differ from selfTestPixel(): the
    embedded PNG must hold exactly those, decoded here with the standard library."""
    script = client.get("/static/similar_images.js").text
    block = script.split("const SELF_TEST_PNG =", 1)[1].split(";", 1)[0]
    png = base64.b64decode("".join(re.findall(r'"([^"]*)"', block)))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    chunks, offset = {}, 8
    while offset < len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        chunks[kind] = chunks.get(kind, b"") + png[offset + 8 : offset + 8 + length]
        offset += 12 + length
    width, height = (
        int.from_bytes(chunks[b"IHDR"][:4], "big"),
        int.from_bytes(chunks[b"IHDR"][4:8], "big"),
    )
    assert (width, height, chunks[b"IHDR"][8], chunks[b"IHDR"][9]) == (8, 8, 8, 2)  # 8-bit RGB
    raw = zlib.decompress(chunks[b"IDAT"])
    for y in range(8):
        line = raw[y * 25 : (y + 1) * 25]
        assert line[0] == 0  # no filter: the bytes are the pixels
        for x in range(8):
            expected = ((x * 31 + y * 7) % 256, (x * 13 + y * 29) % 256, (x * y * 5 + 40) % 256)
            assert tuple(line[1 + x * 3 : 4 + x * 3]) == expected
    formula = re.search(r"function selfTestPixel\(x, y\) \{\s*return (\[.*?\]);", script)
    assert formula is not None
    assert (
        formula.group(1)
        == "[(x * 31 + y * 7) % 256, (x * 13 + y * 29) % 256, (x * y * 5 + 40) % 256]"
    )


GRADIENT = bytes(range(256)).hex() * 4  # a thumbnail with contrast: correlates with itself


def similar_pair(client: TestClient, scan_id: int) -> tuple[int, int]:
    """a.jpg and the XSS-named image look alike; a.jpg has more pixels."""
    ids = entry_ids(client, scan_id)
    big, small = ids[b"photos/a.jpg"], ids[f"photos/{XSS}.jpg".encode()]
    other = ids[f"photos/{FORMULA}.jpg".encode()]
    items = [
        {**signed(big), "thumb": GRADIENT, "width": 4000, "height": 3000},
        {**signed(small), "thumb": GRADIENT, "width": 800, "height": 600},
        {**signed(other, "0" * 64), "thumb": bytes(range(255, -1, -1)).hex() * 4},
    ]
    response = client.post(
        f"/api/scans/{scan_id}/images/signatures", json={"algo": 1, "items": items}, headers=CSRF
    )
    assert response.status_code == 200
    return big, small


def test_similar_images_are_shown_in_groups(client: TestClient, scanned: int) -> None:
    assert "Les groupes apparaîtront ici" in client.get(f"/scans/{scanned}/images").text
    big, small = similar_pair(client, scanned)
    page = client.get(f"/scans/{scanned}/images")
    assert_safe_html(page.text)
    assert "1 groupe(s) parmi 3 image(s)" in page.text
    assert f'src="/api/scans/{scanned}/entries/{big}/image"' in page.text
    assert f'src="/api/scans/{scanned}/entries/{small}/image"' in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text and XSS not in page.text
    assert "4000 \u00d7 3000" in page.text and "la plus grande résolution" in page.text
    assert 'data-version="' in page.text and 'name="keep-1" value="2"' in page.text
    # The fixture's photos hold the same bytes: a likely copy until the
    # duplicates search proves it, then a fact, with the way to that page.
    assert "sans doute une copie identique de l&#39;image gardée" in page.text
    assert "de copies identiques</strong>" in page.text
    run = run_job(client, f"/api/scans/{scanned}/dupes", {"micro": False})
    proven = client.get(f"/scans/{scanned}/images").text
    assert "= copie identique de l&#39;image gardée" in proven
    assert f'href="/dupes/{run["run_id"]}"' in proven


def test_the_similar_images_command_removes_what_is_not_kept(
    client: TestClient, scanned: int
) -> None:
    similar_pair(client, scanned)
    version = re.search(r'data-version="([^"]+)"', client.get(f"/scans/{scanned}/images").text)
    assert version is not None
    url = f"/api/scans/{scanned}/images/command"
    body = {"groups": [1], "action": "trash", "version": version.group(1)}
    data = client.post(url, json=body, headers=CSRF).json()
    assert data["count"] == 1 and data["command"].startswith("gio trash -- \\\n  ")
    assert XSS in data["command"] and "photos/a.jpg" not in data["command"]
    assert any("rafale" in w for w in data["warnings"])
    # The user keeps the smaller one instead: the larger one is proposed.
    kept_small = client.post(url, json={**body, "keep": {"1": 2}}, headers=CSRF).json()
    assert "photos/a.jpg" in kept_small["command"] and XSS not in kept_small["command"]
    for wrong, status in [
        ({"version": "0-0"}, 409),
        ({"groups": [2]}, 422),
        ({"keep": {"1": 3}}, 422),
        ({"action": "archive"}, 422),
    ]:
        assert client.post(url, json={**body, **wrong}, headers=CSRF).status_code == status
    assert client.post(url, json=body).status_code == 403  # CSRF guard


def test_similar_image_pairs_are_cached_then_forgotten(client: TestClient, scanned: int) -> None:
    similar_pair(client, scanned)
    client.get(f"/scans/{scanned}/images")
    data_dir = client.app.state.config.data_dir  # type: ignore[attr-defined]

    def cached() -> list[str]:
        conn = sqlite3.connect(database_path(data_dir))
        try:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT key FROM summary_cache WHERE key LIKE 'similar-images:%'"
                )
            ]
        finally:
            conn.close()

    assert [key.split(":")[1] for key in cached()] == [str(scanned)]
    assert client.post(f"/api/scans/{scanned}/forget", json={}, headers=CSRF).status_code == 200
    assert cached() == []
