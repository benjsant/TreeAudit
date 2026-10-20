from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from treeaudit.core.config import AppConfig, RootConfig
from treeaudit.roots.registry import RootStatus
from treeaudit.roots.validation import RejectReason
from treeaudit.web.app import create_app
from treeaudit.web.security import SECURITY_HEADERS

HOST_PATH = "/mnt/Data/Secret Backup"

STATUSES = (
    RootStatus(root=RootConfig(id="backup", path=HOST_PATH, label="Sauvegarde")),
    RootStatus(
        root=RootConfig(id="home", path="/home/user", label="Maison"),
        reason=RejectReason.HOME_DIRECTORY,
    ),
)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = AppConfig(data_dir=str(tmp_path / "data"), roots=tuple(s.root for s in STATUSES))
    return TestClient(create_app(config, STATUSES), base_url="http://127.0.0.1:8765")


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_security_headers_on_every_response(client: TestClient) -> None:
    for response in (client.get("/health"), client.get("/does-not-exist")):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value


def test_list_roots_exposes_ids_and_labels_but_no_path(client: TestClient) -> None:
    response = client.get("/api/roots")
    assert response.status_code == 200
    roots = response.json()["roots"]
    assert [(r["id"], r["label"], r["accepted"]) for r in roots] == [
        ("backup", "Sauvegarde", True),
        ("home", "Maison", False),
    ]
    assert roots[1]["reason"] == "home_directory"
    assert roots[1]["message"]
    assert HOST_PATH not in response.text
    assert "/home/user" not in response.text
    assert "path" not in roots[0]


def test_get_root_by_id(client: TestClient) -> None:
    response = client.get("/api/roots/backup")
    assert response.status_code == 200
    assert response.json()["id"] == "backup"
    assert HOST_PATH not in response.text


def test_unknown_root_id(client: TestClient) -> None:
    response = client.get("/api/roots/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown root"}


@pytest.mark.parametrize(
    "raw_path",
    [
        "/api/roots/..%2F..%2Fetc",
        "/api/roots/%2Fetc%2Fpasswd",
        "/api/roots/..%2fetc",
        "/api/roots/UPPER",
        "/api/roots/a%00b",
        "/api/roots/" + "a" * 100,
    ],
)
def test_path_like_root_ids_are_rejected_without_echo(client: TestClient, raw_path: str) -> None:
    response = client.get(raw_path)
    assert response.status_code in (404, 422)
    assert "etc" not in response.text
    assert "passwd" not in response.text


def test_foreign_host_header_is_rejected(client: TestClient) -> None:
    response = client.get("/api/roots", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert "backup" not in response.text


def test_rebinding_host_with_port_is_rejected(client: TestClient) -> None:
    response = client.get("/api/roots", headers={"Host": "attacker.example:8765"})
    assert response.status_code == 400


def test_localhost_host_header_is_accepted(client: TestClient) -> None:
    response = client.get("/health", headers={"Host": "localhost:8765"})
    assert response.status_code == 200


def test_no_cors_headers(client: TestClient) -> None:
    get = client.get("/api/roots", headers={"Origin": "https://evil.example"})
    preflight = client.options(
        "/api/roots",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-treeaudit-request",
        },
    )
    for response in (get, preflight):
        assert not any(name.lower().startswith("access-control-") for name in response.headers)


def test_state_changing_request_without_csrf_header_is_forbidden(client: TestClient) -> None:
    assert client.post("/api/roots", json={}).status_code == 403
    form = client.post("/api/roots", data={"a": "b"}, headers={"X-TreeAudit-Request": "1"})
    assert form.status_code == 403


def test_state_changing_request_with_csrf_header_reaches_routing(client: TestClient) -> None:
    response = client.post("/api/roots", json={}, headers={"X-TreeAudit-Request": "1"})
    # No POST route exists yet: the guard let it through to routing.
    assert response.status_code == 405


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_are_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
