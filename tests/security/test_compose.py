"""Checks of the Docker configuration (docs/DECISIONS.md D-010, D-011, D-019)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

FORBIDDEN_SOURCES = frozenset(
    {
        "/",
        "/home",
        "/root",
        "/etc",
        "/proc",
        "/sys",
        "/dev",
        "/run",
        "/var/run",
        "/var/run/docker.sock",
        "/run/docker.sock",
        "~",
        "${HOME}",
        "$HOME",
    }
)


def load(name: str) -> dict[str, Any]:
    with (REPO / name).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


COMPOSE = load("compose.yaml")
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]


def volumes_of(service: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize short and long volume syntaxes to dictionaries."""
    result: list[dict[str, Any]] = []
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            parts = volume.split(":")
            options = parts[2].split(",") if len(parts) > 2 else []
            result.append(
                {
                    "source": parts[0],
                    "target": parts[1] if len(parts) > 1 else parts[0],
                    "read_only": "ro" in options,
                    "selinux": next((o for o in options if o in ("z", "Z")), None),
                }
            )
        else:
            result.append(
                {
                    "source": volume.get("source"),
                    "target": volume.get("target"),
                    "read_only": volume.get("read_only", False),
                    "selinux": (volume.get("bind") or {}).get("selinux"),
                }
            )
    return result


def test_expected_services() -> None:
    assert set(SERVICES) == {"app", "gateway", "check", "check-archive", "dev"}


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_service_hardening(name: str) -> None:
    service = SERVICES[name]
    assert not service.get("privileged", False)
    assert service.get("cap_drop") == ["ALL"]
    assert not service.get("cap_add")
    assert not service.get("devices")
    assert "no-new-privileges:true" in service.get("security_opt", [])
    assert service.get("read_only") is True
    assert 0 < int(service.get("pids_limit", 0)) <= 1024
    for namespace in ("network_mode", "pid", "ipc", "userns_mode"):
        assert service.get(namespace) != "host", namespace
    user = str(service.get("user", ""))
    assert user, "a non-root user must be set"
    assert user.split(":")[0] not in ("0", "root")


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_dangerous_mounts(name: str) -> None:
    for volume in volumes_of(SERVICES[name]):
        source = str(volume["source"]).rstrip("/") or "/"
        assert source not in FORBIDDEN_SOURCES, source
        assert "docker.sock" not in source
        assert volume["selinux"] is None, "':z'/':Z' relabel host files (D-011)"


def test_only_the_gateway_publishes_a_port() -> None:
    publishing = {name for name, service in SERVICES.items() if service.get("ports")}
    assert publishing == {"gateway"}


def test_gateway_port_published_on_loopback_only() -> None:
    ports = SERVICES["gateway"]["ports"]
    assert ports
    for port in ports:
        assert isinstance(port, str)
        assert port.startswith("127.0.0.1:"), port


def test_app_has_no_outbound_network() -> None:
    """D-020: the container that sees the data is only on an internal network."""
    assert SERVICES["app"].get("networks") == ["internal"]
    assert COMPOSE["networks"]["internal"].get("internal") is True


def test_gateway_sees_no_data() -> None:
    gateway = SERVICES["gateway"]
    assert not gateway.get("volumes")
    assert not gateway.get("environment")
    assert set(gateway["networks"]) == {"internal", "edge"}
    assert gateway["command"][0] == "gateway"
    assert gateway["command"][gateway["command"].index("--upstream-host") + 1] == "app"


def test_audited_roots_are_read_only() -> None:
    roots = [v for v in volumes_of(SERVICES["app"]) if str(v["target"]).startswith("/audit/")]
    assert roots, "the app service must mount at least one audited root"
    for volume in roots:
        assert volume["read_only"] is True, volume
        assert str(volume["target"]).startswith("/audit/roots/")


def test_app_writable_mounts_are_only_the_data_dir() -> None:
    writable = [v["target"] for v in volumes_of(SERVICES["app"]) if not v["read_only"]]
    assert writable == ["/data"]


@pytest.mark.parametrize("name", ["check", "check-archive", "dev"])
def test_tool_services_have_no_network(name: str) -> None:
    assert SERVICES[name].get("network_mode") == "none"


@pytest.mark.parametrize("name", ["check", "check-archive"])
def test_check_services_see_no_host_files(name: str) -> None:
    assert not SERVICES[name].get("volumes")
    assert "--no-fs-checks" in SERVICES[name]["entrypoint"]


ARCHIVE_OVERRIDE = load("compose.archive.yaml")


def test_archive_override_only_adds_the_archive_mount_to_app() -> None:
    """D-032: one writable host folder, mounted only in the app container."""
    services = ARCHIVE_OVERRIDE["services"]
    assert set(services) == {"app"}
    app = services["app"]
    assert set(app) == {"environment", "volumes"}
    assert set(app["environment"]) == {"TREEAUDIT_ARCHIVE_DISPLAY_PATH"}
    assert len(app["volumes"]) == 1
    volume = app["volumes"][0]
    assert volume["type"] == "bind"
    assert volume["target"] == "/archives"
    assert volume["source"].startswith("${TREEAUDIT_ARCHIVE_PATH:?")
    assert volume["bind"] == {"create_host_path": False}, "no ':z'/':Z' (D-011)"
    assert not volume.get("read_only", False)


def test_archive_mount_never_targets_a_root_or_the_data_dir() -> None:
    targets = {v["target"] for v in volumes_of(SERVICES["app"])}
    assert "/archives" not in targets
    assert not any(t.startswith("/archives") for t in targets)


def test_docker_config_declares_the_archive_mount_point() -> None:
    text = (REPO / "docker" / "config.toml").read_text(encoding="utf-8")
    assert 'archive_dir = "/archives"' in text


def test_selinux_override_only_disables_labels() -> None:
    override = load("compose.selinux.yaml")
    for name, service in override["services"].items():
        assert set(service) == {"security_opt"}, name
        assert "label=disable" in service["security_opt"]
        assert "no-new-privileges:true" in service["security_opt"]


def test_dockerfile_final_stage_is_not_root() -> None:
    lines = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8").splitlines()
    users = [line.split()[1] for line in lines if line.startswith("USER ")]
    assert users, "the Dockerfile must set a USER"
    assert users[-1].split(":")[0] not in ("0", "root")
    assert any("chmod a-s" in line for line in lines), "setuid bits must be stripped"
    assert not any(":z" in line or ":Z" in line for line in lines if "--mount" in line)


def test_dockerignore_excludes_local_data() -> None:
    entries = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for required in (".env", "data/", ".git"):
        assert required in entries
