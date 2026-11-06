"""Sub-folders proposed by fixed rules (D-050)."""

from __future__ import annotations

import dataclasses
import time

import pytest

from treeaudit.organize.plan import DOCUMENTS, MUSIC, PHOTOS, Move, clean_folder_name
from treeaudit.organize.subfolder_bench import run_rules_bench
from treeaudit.organize.subfolder_cases import HELDOUT_CASES
from treeaudit.organize.subfolders import (
    MAX_SUBFOLDERS,
    _splitter,
    _themes_for,
    is_generic_name,
    propose,
    words_of,
)


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


def test_photos_named_by_a_device_are_not_grouped() -> None:
    found = propose(
        PHOTOS,
        moves(
            "XY_20210305_101112.jpg",
            "XY_20210306_093000.jpg",
            "XY_20210412_181818.jpg",
            "Vacances 001.jpg",
            "Vacances 002.jpg",
            "Vacances 003.jpg",
            "IMG-20210305-WA0012.jpg",
            "IMG-20210412-WA0003.jpg",
            category=PHOTOS,
        ),
    )
    # A camera's prefix says nothing of the picture; a person's series does.
    assert groups(found) == {"WhatsApp": [7, 8], "Vacances": [4, 5, 6]}
    # The same names among documents are a person's choice: they stay grouped.
    assert (
        "XY"
        in propose(DOCUMENTS, moves("XY_20210305_1.pdf", "XY_20210306_2.pdf", "a.pdf")).subfolders
    )


NOON = 1_563_105_600  # 2019-07-14 12:00 UTC


def timed(times: list[int], *names: str, kind: str = "file") -> list[Move]:
    return [
        dataclasses.replace(move, mtime_s=at, kind=kind)
        for move, at in zip(moves(*names, category=PHOTOS), times, strict=True)
    ]


def test_photos_of_one_day_by_the_date_in_their_name() -> None:
    names = [f"IMG_20190714_{h:02d}0000.jpg" for h in range(8, 14)]  # six that day
    names += ["IMG_20190715_100000.jpg", "IMG_20200101_100000.jpg"]
    found = propose(PHOTOS, timed([NOON + 10**8] * 8, *names))  # copied much later
    assert groups(found) == {"2019-07-14": [1, 2, 3, 4, 5, 6]}
    assert found.reasons[1] == "même journée (14/07/2019, date écrite dans le nom)"


def test_too_few_photos_make_no_day() -> None:
    names = [f"IMG_20190714_{h:02d}0000.jpg" for h in range(8, 12)]  # four
    names += [f"IMG_20190801_{h:02d}0000.jpg" for h in range(8, 12)]
    assert groups(propose(PHOTOS, timed([NOON] * 8, *names))) == {}


def test_photos_of_one_day_by_their_modification_date() -> None:
    # Named by numbers only; taken minutes apart that day, and on another day.
    times = [NOON + 300 * i for i in range(6)] + [NOON + 86_400 * 30, NOON + 86_400 * 31]
    found = propose(PHOTOS, timed(times, *(f"{i:04d}.jpg" for i in range(8))))
    assert groups(found) == {"2019-07-14": [1, 2, 3, 4, 5, 6]}
    assert found.reasons[6] == "même journée (14/07/2019, date de modification)"


def test_the_date_of_a_copy_makes_no_day() -> None:
    # A copy that did not keep dates: many files in a few seconds.
    times = [NOON + i // 3 for i in range(12)] + [NOON + 86_400 * 40] * 2
    found = propose(PHOTOS, timed(times, *(f"{i:04d}.jpg" for i in range(14))))
    assert groups(found) == {}


def test_days_never_split_albums_nor_documents() -> None:
    times = [NOON + 300 * i for i in range(6)] + [NOON + 86_400 * 30]
    names = [f"{i:04d}" for i in range(7)]
    assert groups(propose(PHOTOS, timed(times, *names, kind="dir"))) == {}
    documents = [dataclasses.replace(m, category=DOCUMENTS) for m in timed(times, *names)]
    assert groups(propose(DOCUMENTS, documents)) == {}


def test_words_come_before_days() -> None:
    names = [f"Plage Bretagne IMG_20190714_{h:02d}0000.jpg" for h in range(8, 11)]
    names += [f"IMG_20190714_{h:02d}0000.jpg" for h in range(11, 17)]
    names += ["IMG_20200101_100000.jpg"]
    found = propose(PHOTOS, timed([NOON + 10**8] * 10, *names))
    assert groups(found) == {"Plage Bretagne": [1, 2, 3], "2019-07-14": [4, 5, 6, 7, 8, 9]}


def _words(count: int) -> list[str]:
    """Distinct made-up words, letters only (digits never make a word), never
    ending in s or x (plural and singular are grouped together)."""
    last = "abcdefghijklmnopqrtu"
    return ["mot" + chr(97 + i // len(last)) + last[i % len(last)] for i in range(count)]


def test_beyond_the_limit_more_groups_are_announced() -> None:
    names = [f"{word} {n}.pdf" for word in _words(MAX_SUBFOLDERS + 2) for n in "ab"]
    found = propose(DOCUMENTS, moves(*names))
    assert len(found.subfolders) == MAX_SUBFOLDERS and found.more
    assert len(found.by_entry) == 2 * MAX_SUBFOLDERS  # the extra group is not shown
    exact = propose(DOCUMENTS, moves(*names[: 2 * MAX_SUBFOLDERS]))
    assert len(exact.subfolders) == MAX_SUBFOLDERS and not exact.more


def test_the_next_batch_leaves_sorted_elements_and_their_names_alone() -> None:
    names = [f"{word} {n}.pdf" for word in _words(MAX_SUBFOLDERS + 2) for n in "ab"]
    first = propose(DOCUMENTS, moves(*names))
    accepted = [
        dataclasses.replace(move, subfolder=first.by_entry.get(move.entry_id))
        for move in moves(*names)
    ]
    second = propose(DOCUMENTS, accepted)
    assert len(second.subfolders) == 2 and not second.more
    assert not set(second.by_entry) & set(first.by_entry)
    assert not set(second.subfolders) & set(first.subfolders)


def test_a_name_already_taken_is_not_proposed_again() -> None:
    items = moves("Dupont devis.pdf", "Dupont plans.pdf", "Martin a.pdf", "Martin b.pdf", "x.pdf")
    items[0] = dataclasses.replace(items[0], subfolder="Dupont")  # typed by the user
    found = propose(DOCUMENTS, items)
    assert groups(found) == {"Martin": [3, 4]}


def test_a_glued_word_is_cut_when_a_piece_is_a_theme_word() -> None:
    found = propose(DOCUMENTS, moves("FactureEDF.pdf", "facture-sfr.pdf", "notes.txt"))
    assert groups(found) == {"Factures": [1, 2]}
    assert found.reasons[1] == "le nom contient « Facture »"


def test_a_glued_word_is_cut_when_its_pieces_are_words_elsewhere() -> None:
    found = propose(
        DOCUMENTS,
        moves(
            "CompteRendu-mai.docx", "compte rendu juin.docx", "compte rendu juillet.docx", "x.pdf"
        ),
    )
    assert groups(found) == {"Compte Rendu": [1, 2, 3]}


@pytest.mark.parametrize("brand", ["WhatsApp", "YouTube", "PowerPoint"])
def test_a_brand_stays_whole(brand: str) -> None:
    found = propose(
        DOCUMENTS, moves(f"{brand} 1.pdf", f"{brand} 2.pdf", "Apps notes.pdf", "Tube.pdf", "y.pdf")
    )
    assert groups(found) == {brand: [1, 2]}


def test_glued_words_alone_still_group_together() -> None:
    found = propose(DOCUMENTS, moves("CompteRendu-mai.docx", "CompteRendu-juin.docx", "x.pdf"))
    assert groups(found) == {"CompteRendu": [1, 2]}


def test_a_glued_device_prefix_is_still_a_device_series() -> None:
    """Seen on a real folder (D-055): cut, the prefix escaped the filter."""
    names = [f"SunCam_2021030{i}_10111{i}.jpg" for i in range(1, 6)]
    items = moves(*names, "Sun plage.jpg", "Cam voiture.jpg", category=PHOTOS)
    # "Sun" and "Cam" are words elsewhere: the prefix is indeed cut...
    split = _splitter(_themes_for(PHOTOS), [m.source.rsplit("/", 1)[-1] for m in items])
    assert [w for w, _ in words_of(names[0], split)] == ["sun", "cam"]
    # ...and the series is still recognized as a camera's.
    assert propose(PHOTOS, items).by_entry == {}


def test_capitals_alone_are_never_cut() -> None:
    assert words_of("FACTURE-EDF.pdf", lambda pieces: True) == [
        ("facture", "FACTURE"),
        ("edf", "EDF"),
    ]
    assert [w for w, _ in words_of("PDFReport.pdf", lambda pieces: True)] == ["pdf", "report"]


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


@pytest.mark.parametrize(
    ("name", "generic"),
    [
        ("Autres documents divers", True),
        ("Autres_Documents_Importants", True),
        ("Divers", True),
        ("Téléchargements", True),
        ("Archives", True),  # one of the plan's categories
        ("documents", True),
        ("Factures", False),
        ("Impôts", False),
        ("Santé", False),
        ("Formation professionnelle", False),
        ("Documents administratifs", False),
    ],
)
def test_vague_sub_folder_names_are_never_proposed(name: str, generic: bool) -> None:
    """Seen on a real folder and in the benchmark (D-049)."""
    assert is_generic_name(name) is generic
