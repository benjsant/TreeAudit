"""The rules alone on the benchmark's cases, rebuilt as real trees (D-052)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from treeaudit.llm.bench import CASES, Case
from treeaudit.llm.bench_heldout import HELDOUT_CASES
from treeaudit.llm.bench_rules import build_tree, compare, rule_view, run_rules_view
from treeaudit.llm.client import Answer, LlmClient
from treeaudit.parasites.detect import Band


def case(name: str) -> Case:
    return next(c for c in (*CASES, *HELDOUT_CASES) if c.name == name)


def test_a_case_is_rebuilt_as_a_real_tree(tmp_path: Path) -> None:
    folder = build_tree(case("target d'un projet Maven"), tmp_path)
    assert folder == tmp_path / "dev" / "facturation" / "target"
    assert (folder.parent / "pom.xml").is_file() and (folder.parent / "src").is_dir()
    assert (folder / "classes").is_dir() and (folder / "facturation-1.2.jar").is_file()
    assert len(list(folder.glob("*.class"))) == 30  # capped


def test_the_real_rules_see_the_case() -> None:
    view = rule_view(case("target d'un projet Maven"))
    assert view.band is Band.GREEN and view.to_clean and not view.dangerous
    view = rule_view(case("target : objectifs commerciaux"))
    assert view.band is Band.RED and not view.checked


def test_no_folder_to_keep_is_checked_by_the_rules_alone() -> None:
    for view in run_rules_view((*CASES, *HELDOUT_CASES)):
        assert not view.dangerous, view.case.name


class Fixed(LlmClient):
    """Answers "nettoyable" to everything and records what it was asked."""

    def __init__(self) -> None:
        super().__init__("http://llm:8080")
        self.prompts: list[str] = []

    def ask(self, system: str, user: str, schema: Any, *, max_tokens: int = 400) -> Answer:
        self.prompts.append(user)
        data = {
            "contenu_attendu": True,
            "fichiers_personnels": False,
            "avis": "nettoyable",
            "confiance": 90,
            "raisons": ["test"],
        }
        return Answer(data=data, seconds=0.1, tokens=1, tokens_per_second=1.0)


def test_the_ai_is_asked_only_where_the_page_offers_it() -> None:
    views = run_rules_view(
        (
            case("target d'un projet Maven"),  # green: no opinion asked
            case("jquery recopié dans un site"),  # red: asked
            case("jquery téléchargé et décompressé"),  # red: asked
        )
    )
    client = Fixed()
    results = compare(views, client)
    assert [r.asked for r in results] == [False, True, True]
    assert len(client.prompts) == 2
    assert "Bibliothèque web recopiée" in client.prompts[0]  # the real rule's name
    assert results[1].dangerous and results[2].helps
