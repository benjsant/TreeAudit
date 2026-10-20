from __future__ import annotations

import time

import pytest

from treeaudit.filters.spec import (
    MAX_PATTERN_LENGTH,
    FilterError,
    FilterSpec,
    compile_filter,
    compile_regex,
    describe,
    parse_extensions,
    parse_size,
    path_glob_to_regex,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("1024", 1024),
        ("10k", 10 * 1024),
        ("2.5M", int(2.5 * 1024**2)),
        ("1 G", 1024**3),
        ("1Gio", 1024**3),
        ("3GB", 3 * 1024**3),
        ("1t", 1024**4),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "-1", "10x", "1.2.3M"])
def test_invalid_size(text: str) -> None:
    with pytest.raises(FilterError, match="taille"):
        parse_size(text)


def test_parse_extensions() -> None:
    assert parse_extensions("jpg, .PNG,jpeg,,JPG") == ("jpg", "png", "jpeg")
    for bad in ("", " , ", "tar.gz", "a/b"):
        with pytest.raises(FilterError):
            parse_extensions(bad)


# --- globs ----------------------------------------------------------------------------


def path_matches(pattern: str, path: str, *, ignore_case: bool = False) -> bool:
    compiled = compile_filter(FilterSpec(path_glob=pattern, ignore_case=ignore_case))
    return compiled.matches(path.rpartition("/")[2], path)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.py", "a.py", True),
        ("*.py", "src/a.py", False),  # '*' does not cross '/'
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/**/*.py", "src/a.py", True),  # '**' matches zero folders
        ("src/**/*.py", "src/x/y/a.py", True),
        ("**/*.py", "a.py", True),
        ("**/*.py", "deep/er/a.py", True),
        ("src/**", "src/x/y", True),
        ("**", "anything/at/all", True),
        ("*.py", ".hidden.py", True),  # hidden files are matched
        ("**/.venv", "p/.venv", True),
        ("IMG_????.jpg", "IMG_0001.jpg", True),
        ("IMG_????.jpg", "IMG_01.jpg", False),
        ("[ab]*.txt", "b1.txt", True),
        ("[!ab]*.txt", "b1.txt", False),
        ("[!ab]*.txt", "c1.txt", True),
        ("photo (1).jpg", "photo (1).jpg", True),  # brackets and dots are literal
        ("a.b", "axb", False),
        ("x**y", "xabcy", True),  # '**' inside a segment behaves like '*'
        ("x**y", "xa/by", False),
    ],
)
def test_path_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert path_matches(pattern, path) is expected


def test_path_glob_case() -> None:
    assert not path_matches("*.JPG", "a.jpg")
    assert path_matches("*.JPG", "a.jpg", ignore_case=True)


@pytest.mark.parametrize("pattern", ["/etc/*", "a//b", "a/"])
def test_invalid_path_globs(pattern: str) -> None:
    with pytest.raises(FilterError):
        path_glob_to_regex(pattern)


def test_name_glob() -> None:
    compiled = compile_filter(FilterSpec(name_glob="*.py"))
    assert compiled.matches(".hidden.py", "x/.hidden.py")
    assert not compiled.matches("a.pyc", "a.pyc")
    with pytest.raises(FilterError, match="glob de chemin"):
        compile_filter(FilterSpec(name_glob="src/*.py"))


def test_name_exact_and_case() -> None:
    exact = compile_filter(FilterSpec(name_exact="Cour"))
    assert exact.matches("Cour", "a/Cour")
    assert not exact.matches("Cours", "a/Cours")
    assert not exact.matches("cour", "cour")
    folded = compile_filter(FilterSpec(name_exact="ÉTÉ", ignore_case=True))
    assert folded.matches("été", "été")


# --- regexes --------------------------------------------------------------------------


def test_regex_partial_and_full() -> None:
    partial = compile_filter(FilterSpec(name_regex=r"\.pyi?$"))
    assert partial.matches("mod.pyi", "mod.pyi")
    full = compile_filter(FilterSpec(name_regex=r"mod", full_match=True))
    assert not full.matches("module", "module")
    assert full.matches("mod", "mod")


def test_path_regex() -> None:
    compiled = compile_filter(FilterSpec(path_regex=r"(^|/)(\.venv|node_modules)(/|$)"))
    assert compiled.matches("x.js", "app/node_modules/x.js")
    assert not compiled.matches("node_modules.txt", "docs/node_modules.txt")


def test_regex_ignore_case_is_unicode_aware() -> None:
    compiled = compile_filter(FilterSpec(name_regex="^été", ignore_case=True))
    assert compiled.matches("ÉTÉ 2024.jpg", "ÉTÉ 2024.jpg")


@pytest.mark.parametrize(("pattern", "message"), [("[abc", "position 0"), ("(a", "position 0")])
def test_invalid_regex_message(pattern: str, message: str) -> None:
    with pytest.raises(FilterError, match=message):
        compile_filter(FilterSpec(name_regex=pattern))


@pytest.mark.parametrize(
    "pattern",
    [
        "(a+)+$",
        "(a*)*",
        r"(\w+\s?)+$",
        "(.*a){10}",
        "(a|aa)*$",
        "(x+x+)+y",
        "((ab)*)+",
        "(?:a+){2,}",
    ],
)
def test_catastrophic_regexes_are_refused(pattern: str) -> None:
    with pytest.raises(FilterError, match="refusée"):
        compile_regex(pattern, ignore_case=False, what="regex de nom")


@pytest.mark.parametrize(
    "pattern",
    [
        r".*\.pyi?$",
        r".*\.(png|jpe?g)$",
        r"(^|/)(\.venv|__pycache__|node_modules|build|dist)(/|$)",
        r".*202[3-6].*",
        r"^IMG_\d{4}\.jpg$",
        r"backup-\d+",
    ],
)
def test_usual_regexes_are_accepted(pattern: str) -> None:
    compile_regex(pattern, ignore_case=True, what="regex de nom")


def test_refused_pattern_would_really_be_slow() -> None:
    """Sanity check of the threat: a refused pattern takes ages on a short name."""
    import re

    pattern = re.compile("(a+)+$")
    start = time.perf_counter()
    pattern.search("a" * 22 + "!")
    assert time.perf_counter() - start > 0.05


def test_pattern_length_and_nul() -> None:
    with pytest.raises(FilterError, match="trop long"):
        compile_filter(FilterSpec(name_regex="a" * (MAX_PATTERN_LENGTH + 1)))
    with pytest.raises(FilterError, match="nul"):
        compile_filter(FilterSpec(name_glob="a\x00b"))
    with pytest.raises(FilterError, match="vide"):
        compile_filter(FilterSpec(name_regex=""))


def test_size_bounds() -> None:
    with pytest.raises(FilterError, match="supérieure"):
        compile_filter(FilterSpec(min_size=10, max_size=5))
    with pytest.raises(FilterError, match="négative"):
        compile_filter(FilterSpec(min_size=-1))


def test_describe() -> None:
    text = describe(
        FilterSpec(extensions=("jpg",), name_regex="^IMG", ignore_case=True, min_size=10 * 1024**2)
    )
    assert (
        text == "extension .jpg ET regex de nom '^IMG' (partiel) ET taille ≥ 10 Mio ; casse ignorée"
    )
    assert describe(FilterSpec()) == "aucun critère (tout le scan)"


def test_name_contains_ignores_case() -> None:
    compiled = compile_filter(FilterSpec(name_contains="Facture"))
    assert compiled.matches("facture-2024.pdf", "docs/facture-2024.pdf")
    assert compiled.matches("MA FACTURE.PDF", "MA FACTURE.PDF")
    assert not compiled.matches("devis.pdf", "factures/devis.pdf")  # the name, not the path
    assert describe(FilterSpec(name_contains="facture")) == "nom contenant 'facture'"
