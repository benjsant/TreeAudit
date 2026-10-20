from __future__ import annotations

import shlex
from typing import Any

import pytest

from treeaudit.parasites.commands import Action, cleanup_command, recreate_command
from treeaudit.parasites.detect import Band, Finding, band_of
from treeaudit.parasites.report import SelectionError, parse_selection
from treeaudit.parasites.rules import (
    Recreate,
    Rule,
    RuleError,
    Target,
    load_default_rules,
    parse_rules,
)

# --- catalogue ------------------------------------------------------------------------


def test_default_catalogue_loads_and_is_consistent() -> None:
    rules = load_default_rules()
    assert len(rules) >= 40
    ids = [rule.id for rule in rules]
    assert len(ids) == len(set(ids))
    for rule in rules:
        assert rule.label and rule.explanation
        assert 0 <= rule.base <= rule.max_confidence <= 100


def test_catalogue_covers_the_usual_suspects() -> None:
    names = {name for rule in load_default_rules() for name in rule.names}
    for expected in ("__pycache__", ".venv", "node_modules", "__MACOSX", ".DS_Store", "Thumbs.db"):
        assert expected in names


def rule_table(**overrides: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "id": "demo",
        "label": "Démo",
        "target": "dir",
        "names": ["demo"],
        "base": 50,
        "explanation": "pour les tests",
    }
    table.update(overrides)
    return table


def test_minimal_rule() -> None:
    [rule] = parse_rules({"rule": [rule_table()]})
    assert (rule.id, rule.target, rule.names, rule.max_confidence) == (
        "demo",
        Target.DIR,
        ("demo",),
        100,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target": "symlink"}, "target"),
        ({"names": [], "globs": []}, "at least one name or glob"),
        ({"names": ["a/b"]}, "invalid name"),
        ({"names": [".."]}, "invalid name"),
        ({"globs": ["[!a]*"]}, "invalid glob"),
        ({"globs": ["dir/*"]}, "invalid glob"),
        ({"base": 101}, "base"),
        ({"base": True}, "base"),
        ({"surprise": 1}, "unknown key"),
        ({"id": "Bad Id"}, "invalid id"),
        ({"evidence": [{"kind": "nope", "value": "x", "weight": 1, "label": "l"}]}, "kind"),
        ({"evidence": [{"kind": "child", "value": "x", "weight": 500, "label": "l"}]}, "weight"),
        (
            {"evidence": [{"kind": "child_glob", "value": "[^x]", "weight": 1, "label": "l"}]},
            "glob",
        ),
        ({"recreate": [{"command": "rm -rf {path}"}]}, "placeholder"),
        ({"recreate": [{"command": "x", "when_sibling": "a/b"}]}, "invalid name"),
    ],
)
def test_invalid_rules(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(RuleError, match=message):
        parse_rules({"rule": [rule_table(**overrides)]})


def test_duplicate_rule_ids() -> None:
    with pytest.raises(RuleError, match="duplicate"):
        parse_rules({"rule": [rule_table(), rule_table()]})


def test_bands() -> None:
    assert [band_of(c) for c in (100, 80, 79, 50, 49, 0)] == [
        Band.GREEN,
        Band.GREEN,
        Band.ORANGE,
        Band.ORANGE,
        Band.RED,
        Band.RED,
    ]


# --- commands -------------------------------------------------------------------------


def finding(rel_path: bytes, *, number: int = 1, confidence: int = 90, **kwargs: Any) -> Finding:
    name = rel_path.rpartition(b"/")[2]
    rule = Rule(
        id="demo",
        label="Démo",
        target=Target.DIR,
        names=("demo",),
        globs=(),
        base=confidence,
        explanation="x",
        recreate=kwargs.pop("recreate", ()),
    )
    return Finding(
        rule=rule,
        entry_id=number,
        depth=rel_path.count(b"/") + 1,
        name=name,
        name_display=name.decode("utf-8", "backslashreplace"),
        rel_path=rel_path,
        rel_path_display=rel_path.decode("utf-8", "backslashreplace"),
        confidence=confidence,
        evidence=(),
        allocated_size=4096,
        files=1,
        recreate=rule.recreate[0] if rule.recreate else None,
        number=number,
    )


def split_command(text: str) -> list[str]:
    """What the shell will actually receive, after line continuations."""
    return shlex.split(text.replace(" \\\n  ", " "))


HOSTILE = [
    b"with space/node_modules",
    b"quote'here/node_modules",
    b"$(touch pwned)/node_modules",
    b"`id`/node_modules",
    b"-rf/node_modules",
    b"semi;colon && x/node_modules",
    b"\xc3\xa9t\xc3\xa9/node_modules",  # "été", valid UTF-8
]


@pytest.mark.parametrize("action", [Action.TRASH, Action.DELETE])
def test_paths_reach_the_command_literally(action: Action) -> None:
    findings = [finding(p, number=i) for i, p in enumerate(HOSTILE, start=1)]
    result = cleanup_command(action, findings, "/mnt/disk with space")
    assert result.text is not None and result.skipped == ()
    argv = split_command(result.text)
    program = ["gio", "trash", "--"] if action is Action.TRASH else ["rm", "-rf", "--"]
    assert argv[:3] == program
    assert argv[3:] == [f"/mnt/disk with space/{p.decode()}" for p in HOSTILE]


def test_command_is_multiline_with_backslashes() -> None:
    result = cleanup_command(Action.TRASH, [finding(b"a/x"), finding(b"b/y")], "/r")
    assert result.text == "gio trash -- \\\n  /r/a/x \\\n  /r/b/y"


@pytest.mark.parametrize(
    "rel_path", [b"new\nline/node_modules", b"tab\there/x", b"bad\xff/node_modules", b"esc\x1b/x"]
)
def test_unsafe_paths_get_no_command(rel_path: bytes) -> None:
    unsafe = finding(rel_path, number=2)
    result = cleanup_command(Action.DELETE, [finding(b"ok/x"), unsafe], "/r")
    assert result.skipped == (unsafe,)
    assert result.text is not None
    assert split_command(result.text) == ["rm", "-rf", "--", "/r/ok/x"]


def test_only_unsafe_paths_gives_no_command() -> None:
    result = cleanup_command(Action.TRASH, [finding(b"bad\nname")], "/r")
    assert result.text is None


@pytest.mark.parametrize("root", ["relative/root", "/root\nwith newline", ""])
def test_unsafe_root_gives_no_command(root: str) -> None:
    result = cleanup_command(Action.TRASH, [finding(b"a")], root)
    assert result.text is None


def test_archive_command() -> None:
    result = cleanup_command(
        Action.ARCHIVE, [finding(b"-dash/node_modules")], "/mnt/d", archive_name="scan1.tar.gz"
    )
    assert result.text is not None
    argv = split_command(result.text)  # "$HOME/..." stays a literal for shlex
    assert argv[argv.index("--directory") + 1] == "/mnt/d"
    assert argv[-2:] == ["--", "./-dash/node_modules"]


def test_archive_name_is_validated() -> None:
    with pytest.raises(ValueError, match="archive name"):
        cleanup_command(Action.ARCHIVE, [finding(b"a")], "/r", archive_name="../x; rm")


def test_recreate_command_quotes_name_and_parent() -> None:
    target = finding(
        b"my project/.venv",
        recreate=(Recreate(command="python3 -m venv {name} && {name}/bin/pip install ."),),
    )
    command = recreate_command(target, "/mnt/d")
    assert command == (
        "cd -- '/mnt/d/my project' && python3 -m venv .venv && .venv/bin/pip install ."
    )


def test_recreate_command_absent_for_unsafe_path() -> None:
    target = finding(b"bad\n/.venv", recreate=(Recreate(command="python3 -m venv {name}"),))
    assert recreate_command(target, "/mnt/d") is None


# --- selection --------------------------------------------------------------------------


@pytest.fixture
def numbered() -> list[Finding]:
    return [
        finding(b"a", number=1, confidence=95),
        finding(b"b", number=2, confidence=85),
        finding(b"c", number=3, confidence=60),
        finding(b"d", number=4, confidence=40),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1", [1]), ("3,1", [1, 3]), ("2-4", [2, 3, 4]), ("green", [1, 2]), ("vert,4", [1, 2, 4])],
)
def test_selection(numbered: list[Finding], text: str, expected: list[int]) -> None:
    assert [f.number for f in parse_selection(text, numbered)] == expected


@pytest.mark.parametrize("text", ["", "x", "5", "3-1", "1-9", "red"])
def test_invalid_selection(numbered: list[Finding], text: str) -> None:
    with pytest.raises(SelectionError):
        parse_selection(text, numbered)
