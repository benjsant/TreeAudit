"""Sub-folders proposed by fixed rules (D-050)."""

from __future__ import annotations

import time

import pytest

from treeaudit.organize.plan import DOCUMENTS, MUSIC, PHOTOS, Move, clean_folder_name
from treeaudit.organize.subfolder_bench import run_rules_bench
from treeaudit.organize.subfolder_cases import HELDOUT_CASES
from treeaudit.organize.subfolders import MAX_SUBFOLDERS, is_generic_name, propose, words_of


def moves(*names: str, category: str = DOCUMENTS) -> list[Move]:
    return [
        Move(
            entry_id=index,
            source=f"Bazar/{name}",
            target=f"Rangé/{category}/{name}",
            kind="file",
            category=category,
            size=1000,
            files=1,
            mtime_s=1_600_000_000,
            reason="fichier rangé par type",
        )
        for index, name in enumerate(names, start=1)
    ]


def groups(found: object) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for entry, sub in sorted(found.by_entry.items()):  # type: ignore[attr-defined]
        out.setdefault(sub, []).append(entry)
    return out


def test_words_drop_case_accents_digits_and_filler() -> None:
    assert [w for w, _ in words_of("Relevé_Bancaire-2023 (copie) V2.PDF")] == ["releve", "bancaire"]
    assert words_of("TD1_maths.pdf") == [("td", "TD"), ("maths", "maths")]
    assert words_of("Cœur de pirate.mp3") == [("coeur", "Cœur"), ("pirate", "pirate")]
    assert words_of("IMG_4521.JPG") == []
    assert words_of(".bashrc") == [("bashrc", "bashrc")]


def test_themes_and_shared_words() -> None:
    found = propose(
        DOCUMENTS,
        moves(
            "facture-edf.pdf",
            "Facture SFR.pdf",
            "avis_imposition_2022.pdf",
            "Avis d'impôt 2023.pdf",
            "Dupont devis.pdf",
            "Dupont plans.pdf",
            "notes.txt",
        ),
    )
    assert groups(found) == {"Factures": [1, 2], "Impôts": [3, 4], "Dupont": [5, 6]}
    assert found.by_rules
    assert found.reasons[1] == "le nom contient « facture »"
    assert found.reasons[5] == "mot en commun : « Dupont »"
    assert 7 not in found.by_entry


def test_a_subject_wins_over_a_kind_of_paper() -> None:
    found = propose(
        DOCUMENTS,
        moves(
            "facture-garage-vidange.pdf", "carte-grise.pdf", "facture-edf.pdf", "facture-sfr.pdf"
        ),
    )
    assert groups(found) == {"Factures": [3, 4], "Voiture": [1, 2]}


def test_a_lone_theme_member_is_left_to_shared_words() -> None:
    found = propose(DOCUMENTS, moves("Devis Dupont.pdf", "Dupont - facture acompte.pdf", "x.pdf"))
    assert groups(found) == {"Dupont": [1, 2]}


def test_a_sub_folder_holding_everything_is_never_proposed() -> None:
    found = propose(
        DOCUMENTS,
        moves(
            "facture-EDF-01.pdf", "facture-EDF-02.pdf", "facture-SFR-01.pdf", "facture-SFR-02.pdf"
        ),
    )
    assert groups(found) == {"EDF": [1, 2], "SFR": [3, 4]}


def test_names_by_category() -> None:
    photos = propose(
        PHOTOS, moves("Screenshot_1.png", "Capture d'écran 2.png", "a.jpg", category=PHOTOS)
    )
    assert photos.subfolders == ("Captures d'écran",)
    # Paper themes do not apply to music: "Banque" is not a band's name here.
    music = propose(
        MUSIC,
        moves(
            "Daft Punk - One.mp3", "daft_punk-two.mp3", "Facture - 1.mp3", "x.mp3", category=MUSIC
        ),
    )
    assert groups(music) == {"Daft Punk": [1, 2]}


def test_proposed_names_are_safe_and_never_generic() -> None:
    found = propose(
        DOCUMENTS,
        moves(
            "documents divers 1.pdf",
            "documents divers 2.pdf",
            "..pdf",
            "-rf documents.pdf",
            "Téléchargements 1.pdf",
            "Téléchargements 2.pdf",
            "Documents 3.pdf",
        ),
    )
    for name in found.subfolders:
        assert clean_folder_name(name) == name
        assert not is_generic_name(name)
    assert "Divers" not in found.subfolders


def test_same_input_same_answer() -> None:
    names = [f"{word}-{index}.pdf" for word in ("alpha", "beta", "gamma") for index in range(3)]
    assert propose(DOCUMENTS, moves(*names)) == propose(DOCUMENTS, moves(*names))


def test_many_elements_quickly_and_bounded() -> None:
    def client(index: int) -> str:  # 400 different words: digits never make a word
        return "client" + "".join(chr(97 + index // 26**k % 26) for k in range(2))

    names = [f"{client(index % 400)} rapport {index}.pdf" for index in range(50_000)]
    started = time.monotonic()
    found = propose(DOCUMENTS, moves(*names))
    assert time.monotonic() - started < 10
    assert len(found.subfolders) <= MAX_SUBFOLDERS


@pytest.mark.parametrize("case", HELDOUT_CASES, ids=lambda case: case.name)
def test_held_out_cases_stay_sorted(case: object) -> None:
    [result] = run_rules_bench([case])  # type: ignore[list-item]
    assert result.names_valid and not result.generic
    assert result.score >= 0.95
