"""Sub-folders proposed by fixed rules for one category of a tidying plan (D-050).

Deterministic and explainable, for everyone (no AI, no graphics card):
1. themes: a name holding a known word ("facture", "impôts", "payslip"…) goes
   to that theme's sub-folder; a subject ("garage": the car) wins over a kind
   of paper ("facture");
2. shared words: among what is left, the names sharing a rare word ("maths",
   "Dupont", "Daft Punk") are grouped under that word, the most shared first.
A sub-folder holds at least two elements and never all of them (it would sort
nothing); its name goes through the same checks as a name typed by the user.
Photos and videos are already sorted by year by the plan itself. Nothing
changes until the user accepts the proposals (organize/plan.py).
"""

from __future__ import annotations

import functools
import heapq
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from treeaudit.organize.plan import (
    CATEGORIES,
    MUSIC,
    PHOTOS,
    PROJECTS,
    SOFTWARE,
    VIDEOS,
    Move,
    clean_folder_name,
)

MAX_SUBFOLDERS = 30
NO_SUBFOLDERS = frozenset({PROJECTS, SOFTWARE})
"""Moved whole, as they are: never split into sub-folders."""
MIN_MEMBERS = 2
MAX_NAME_WORDS = 2
NONE = "aucun"

# Names that say nothing of the content: where the files came from, or one of
# the plan's own categories (D-049, seen on a real folder).
GENERIC_NAMES = frozenset(
    name.casefold()
    for name in (
        *CATEGORIES,
        "Téléchargements",
        "Telechargements",
        "Downloads",
        "Bureau",
        "Desktop",
        "Divers",
        "Fichiers",
        "Dossiers",
        "Nouveau dossier",
        "Temp",
        "Général",
        "General",
        "Misc",
        "Supports",
        NONE,
    )
)
# A name made of these words only, or starting with one ("Autres documents
# divers"), sorts nothing: it is what a model writes when it gives up.
_VAGUE_WORDS = frozenset({"autre", "autres", "divers", "diverses", "misc", "varié", "variés"})


def is_generic_name(name: str) -> bool:
    """A name that says nothing of the content (D-049): never proposed."""
    words = name.replace("_", " ").replace("-", " ").casefold().split()
    if not words:
        return True
    filler = _VAGUE_WORDS | {"document", "documents", "fichier", "fichiers"}
    return (
        name.casefold() in GENERIC_NAMES
        or " ".join(words) in GENERIC_NAMES
        or words[0] in _VAGUE_WORDS
        or all(word in filler for word in words)
    )


@dataclass(frozen=True, slots=True)
class Suggestions:
    category: str
    subfolders: tuple[str, ...]
    by_entry: dict[int, str]
    """Entry id -> suggested sub-folder (elements without one are absent)."""
    considered: int
    total: int
    reasons: Mapping[int, str] = field(default_factory=dict)
    """Entry id -> why, for the rules (the AI gives none)."""
    by_rules: bool = False


@dataclass(frozen=True, slots=True)
class _Theme:
    folder: str
    words: frozenset[str]
    """Normalized words, or two words separated by a space for a phrase. A word
    of 5 letters or more also matches the longer words it starts."""
    subject: bool
    """What the paper is about (it wins) rather than what kind of paper it is."""


def _theme(folder: str, words: str, *, subject: bool = True) -> _Theme:
    return _Theme(folder, frozenset(w.replace("_", " ") for w in words.split()), subject)


_PAPER_THEMES = (
    _theme("Factures", "factur invoice bill echeancier", subject=False),
    _theme("Impôts", "impot imposition fiscal taxe tax revenu"),
    _theme("Salaires", "salaire paie paye payslip payroll"),
    _theme("Banque", "banque bancaire bank releve rib iban pret emprunt amortissement"),
    _theme(
        "Santé",
        "sante medical medecin ordonnance mutuelle dentiste securite_sociale secu cpam ameli "
        "vitale hopital irm radiologie laboratoire sang vaccin pharmacie health",
    ),
    _theme(
        "Emploi",
        "cv curriculum resume motivation candidature embauche recrutement pole_emploi "
        "france_travail contrat_travail cdi cdd",
    ),
    _theme(
        "Logement",
        "bail loyer quittance etat_lieux habitation appartement proprietaire locataire rent lease",
    ),
    _theme(
        "Voiture",
        "voiture auto carte_grise permis garage vidange controle_technique vehicule "
        "immatriculation pneu",
    ),
    _theme("Notices", "notice manuel manual garantie warranty", subject=False),
)
_PICTURE_THEMES = (
    _theme("Captures d'écran", "screenshot capture screen_recording", subject=False),
    _theme("WhatsApp", "whatsapp wa", subject=False),
)


def _themes_for(category: str) -> tuple[_Theme, ...]:
    if category in (PHOTOS, VIDEOS):
        return _PICTURE_THEMES
    if category == MUSIC:
        return ()
    return _PAPER_THEMES


# Words that tell nothing of what a file is about: never a reason to group.
_STOP_WORDS = frozenset(
    word
    for line in (
        "a au aux avec ce d de des du en et l la le les ma mes mon ne par pour",
        "sa ses son sur un une",
        "a an and at by for in is it my of on or the to with",
        "copie copy final finale new nouveau nouvelle old ancien version draft brouillon",
        "doc document fichier file dossier folder untitled sans nom titre export test scan",
        "img image dsc pxl photo pic picture video mov vid track piste",
        "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre",
        "jan fev feb mar apr avr jun jul aug sep sept oct nov dec",
        "january february march april may june july august september october november december",
        "ans",
    )
    for word in line.split()
)
_WORD_BREAKS = re.compile(r"[\W_]+")
_SEPARATORS = re.compile(r"[^0-9a-z]+")
_DIGITS = re.compile(r"[0-9]+")


def _stem(name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    return stem if dot and stem and 0 < len(extension) <= 5 else name


def _plain(text: str) -> str:
    folded = text.casefold().replace("œ", "oe").replace("æ", "ae")
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def words_of(name: str) -> list[tuple[str, str]]:
    """(normalized word, word as written) of a file name, in order.

    Case, accents and digits are dropped ("TD1" -> "td", "Réseaux" ->
    "reseaux"); words that say nothing of the content are left out.
    """
    out: list[tuple[str, str]] = []
    for part in _WORD_BREAKS.split(_stem(name)):
        written = _DIGITS.sub("", part)
        for piece in _SEPARATORS.split(_plain(written)):
            if len(piece) >= 2 and piece not in _STOP_WORDS:
                out.append((piece, written))
    return out


def _key(word: str) -> str:
    """Plural and singular together: "reseaux" and "reseau", "maths" and "math"."""
    return word[:-1] if len(word) > 4 and word[-1] in "sx" else word


@dataclass(frozen=True, slots=True)
class _ThemeIndex:
    """Keywords -> positions of their themes, built once: a name is matched in
    a few dictionary lookups per word, not by trying every keyword."""

    themes: tuple[_Theme, ...]
    exact: dict[str, tuple[int, ...]]
    prefixes: dict[str, tuple[int, ...]]
    lengths: tuple[int, ...]
    phrases: dict[str, tuple[int, ...]]


@functools.cache
def _index(themes: tuple[_Theme, ...]) -> _ThemeIndex:
    exact: dict[str, list[int]] = defaultdict(list)
    prefixes: dict[str, list[int]] = defaultdict(list)
    phrases: dict[str, list[int]] = defaultdict(list)
    for position, theme in enumerate(themes):
        for keyword in theme.words:
            if " " in keyword:
                phrases[keyword].append(position)
            else:
                exact[keyword].append(position)
                if len(keyword) >= 5:
                    prefixes[keyword].append(position)
    return _ThemeIndex(
        themes,
        {k: tuple(v) for k, v in exact.items()},
        {k: tuple(v) for k, v in prefixes.items()},
        tuple(sorted({len(k) for k in prefixes})),
        {k: tuple(v) for k, v in phrases.items()},
    )


def _best_theme(
    themes: tuple[_Theme, ...], pairs: Sequence[tuple[str, str]]
) -> tuple[_Theme, str] | None:
    """The theme with the most matching words, a subject counting double.

    A keyword of 5 letters or more also matches the longer words it starts;
    a phrase matches two words in a row. Returns the theme and its first
    matching word, as written in the name.
    """
    index = _index(themes)
    hits: dict[int, list[str]] = defaultdict(list)
    words = [w for w, _ in pairs]
    for position, word in enumerate(words):
        by_phrase: tuple[int, ...] = ()
        if position + 1 < len(words):
            by_phrase = index.phrases.get(f"{word} {words[position + 1]}", ())
            for theme in by_phrase:
                hits[theme].append(f"{pairs[position][1]} {pairs[position + 1][1]}")
        found = set(index.exact.get(word, ()))
        for length in index.lengths:
            if len(word) > length:
                found.update(index.prefixes.get(word[:length], ()))
        for theme in found.difference(by_phrase):
            hits[theme].append(pairs[position][1])
    if not hits:
        return None
    best = max(
        hits,
        key=lambda t: ((2 if index.themes[t].subject else 1) * len(hits[t]), -t),
    )
    return index.themes[best], hits[best][0]


def _display(forms: Counter[str]) -> str:
    """The word as most often written: accented, then capitalized, spellings win."""
    form = max(
        forms.items(),
        key=lambda item: (
            not item[0].isascii(),
            item[1],
            item[0][:1].isupper() and not item[0].isupper(),
        ),
    )[0]
    return form[:1].upper() + form[1:] if form.islower() else form


def _accept(name: str, used: set[str], category: str) -> str | None:
    clean = clean_folder_name(name)
    if (
        clean is None
        or is_generic_name(clean)
        or clean.casefold() == category.casefold()
        or clean.casefold() in used
    ):
        return None
    return clean


def propose(category: str, moves: Sequence[Move]) -> Suggestions:
    """Sub-folders for the elements of one category of the plan, by fixed rules."""
    names = {m.entry_id: m.source.rsplit("/", 1)[-1] for m in moves}
    words = {entry: words_of(name) for entry, name in names.items()}
    count = len(moves)
    by_entry: dict[int, str] = {}
    reasons: dict[int, str] = {}
    used: set[str] = set()
    order: list[str] = []

    def keep(folder: str, members: Sequence[int]) -> str | None:
        """The sub-folder's name if it is kept: enough members, but not all."""
        if len(order) >= MAX_SUBFOLDERS or not MIN_MEMBERS <= len(members) < count:
            return None
        name = _accept(folder, used, category)
        if name is not None:
            used.add(name.casefold())
            order.append(name)
            for entry in members:
                by_entry[entry] = name
        return name

    # 1. Themes. Fewer than two members: left to the shared words.
    themed: dict[str, list[int]] = defaultdict(list)
    matched: dict[int, str] = {}
    themes = _themes_for(category)
    for entry, pairs in words.items():
        found = _best_theme(themes, pairs)
        if found is not None:
            themed[found[0].folder].append(entry)
            matched[entry] = found[1]
    for folder, members in sorted(themed.items(), key=lambda item: (-len(item[1]), item[0])):
        if keep(folder, members) is not None:
            for entry in members:
                reasons[entry] = f"le nom contient « {matched[entry]} »"

    # 2. Shared words, the most shared first (a heap with lazy recounts).
    holders: dict[str, set[int]] = defaultdict(set)
    forms: dict[str, Counter[str]] = defaultdict(Counter)
    for entry, pairs in words.items():
        if entry in by_entry:
            continue
        for word, written in pairs:
            holders[_key(word)].add(entry)
            forms[_key(word)][written] += 1
    heap = [(-len(e), -len(k), k) for k, e in holders.items() if len(e) >= MIN_MEMBERS]
    heapq.heapify(heap)
    while heap and len(order) < MAX_SUBFOLDERS:
        size, length, key = heapq.heappop(heap)
        group = holders[key] - by_entry.keys()
        if len(group) < MIN_MEMBERS:
            continue
        if len(group) < -size:  # some were taken since: put it back at its true rank
            heapq.heappush(heap, (-len(group), length, key))
            continue
        # Words every member shares make the name, in the order they are
        # written: "Daft Punk", not "Daft"; "Cours Python", not "Chapitre".
        # A word every element shares tells them apart from nothing: "EDF",
        # not "Facture EDF", in a folder of bills.
        first = names[min(group)]
        shared = [
            k
            for k in dict.fromkeys(_key(w) for w, _ in words_of(first))
            if k == key or (len(k) >= 3 and group <= holders[k] and len(holders[k]) < count)
        ][:MAX_NAME_WORDS]
        name = keep(" ".join(_display(forms[k]) for k in shared), sorted(group))
        for entry in group if name is not None else ():
            reasons[entry] = f"mot en commun : « {name} »"

    return Suggestions(category, tuple(order), by_entry, count, count, reasons, by_rules=True)
