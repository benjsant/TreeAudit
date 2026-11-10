"""The release workflow (D-062): pinned, least privilege, a draft only."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"


def load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def steps() -> list[dict[str, Any]]:
    jobs = load(WORKFLOWS / "release.yml")["jobs"]
    return [step for job in jobs.values() for step in job["steps"]]


def test_the_release_workflow_is_the_only_one() -> None:
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["release.yml"]


def test_every_action_is_pinned_by_commit_and_keeps_no_credentials() -> None:
    used = [step for step in steps() if "uses" in step]
    assert [step["uses"].split("@")[0] for step in used] == ["actions/checkout"]
    for step in used:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
        assert step["with"]["persist-credentials"] is False


def test_least_privilege_and_only_tags_or_a_manual_trial() -> None:
    workflow = load(WORKFLOWS / "release.yml")
    # PyYAML reads the key "on" as True.
    assert workflow[True] == {"push": {"tags": ["v[0-9]*"]}, "workflow_dispatch": {}}
    assert workflow["permissions"] == {}
    for job in workflow["jobs"].values():
        assert job["permissions"] == {"contents": "write"}


def test_github_values_never_reach_the_shell_directly() -> None:
    """A tag is chosen by whoever pushes it: through the environment only."""
    for step in steps():
        assert "${{" not in step.get("run", ""), step["name"]


def test_the_tag_is_checked_first_and_the_release_is_a_draft() -> None:
    names = [step["name"] for step in steps()]
    assert names.index("The tag names the version written in the code") < names.index(
        "Prerequisites"
    )
    release = next(step for step in steps() if "gh release create" in step.get("run", ""))
    assert release["if"] == "github.ref_type == 'tag'"
    assert "--draft" in release["run"] and "--verify-tag" in release["run"]
    assert "dist/SHA256SUMS" in release["run"]
    for forbidden in ("gh release edit", "--latest", "publish"):
        assert forbidden not in release["run"]
