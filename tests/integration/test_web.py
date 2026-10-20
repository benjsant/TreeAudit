"""Web interface: pages, JSON API, jobs, commands and exports, with hostile names."""

from __future__ import annotations

import csv
import io
import re
import threading
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.roots.mountinfo import MountTable
from treeaudit.roots.registry import evaluate_roots
from treeaudit.roots.validation import RejectReason
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
    assert "Disque &lt;test&gt;" in response.text
    assert "script-src" not in response.headers["content-security-policy"]  # default-src 'self'
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert_safe_html(response.text)


def test_static_files(client: TestClient) -> None:
    assert client.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/static/app.css").headers["content-type"].startswith("text/css")
    script = client.get("/static/app.js").text
    assert not re.search(r"\.(inner|outer)HTML\s*=|insertAdjacentHTML|document\.write", script)


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
    match = re.search(rf'href="/scans/{scanned}\?dir=(\d+)">📁 photos', page.text)
    assert match is not None
    inside = client.get(f"/scans/{scanned}?dir={match.group(1)}")
    assert inside.status_code == 200 and "a.jpg" in inside.text
    assert_safe_html(inside.text)
    assert client.get(f"/scans/{scanned}?dir=999999").status_code == 404
    assert client.get("/scans/999").status_code == 404
    assert "<h1>Erreur 404</h1>" in client.get("/scans/999").text


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
    assert "G1" in page.text
    assert_safe_html(page.text)

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
    assert "3 résultat(s)" in page.text
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
    for url in ("/", f"/scans/{scanned}", f"/scans/{scanned}/parasites", "/scan/new?root=main"):
        assert str(root) not in client.get(url).text


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
