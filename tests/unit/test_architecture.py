"""Which part of TreeAudit may import which (docs/ARCHITECTURE.md, "Règles de dépendance").

The imports are read from the source, never executed. A new dependency
between packages makes this test fail until it is added to ALLOWED on
purpose, with the reason it is needed.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "treeaudit"

# Package (or top-level module) -> what it may import. Inside one package,
# anything goes.
ALLOWED: dict[str, set[str]] = {
    "__main__": {"entry"},
    "entry": {"cli", "core"},
    "cli": {
        "archive",
        "bench",
        "core",
        "db",
        "duplicates",
        "filters",
        "gateway",
        "organize",
        "parasites",
        "roots",
        "scanner",
        "shellcmd",
        "web",
    },
    "web": {
        "archive",
        "core",
        "db",
        "duplicates",
        "filters",
        "organize",
        "parasites",
        "preview",
        "projects",
        "roots",
        "scanner",
        "shellcmd",
        "similar",
    },
    "gateway": {"core"},
    "archive": {"core", "duplicates", "roots", "scanner"},
    "db": {"scanner"},
    "duplicates": {"core", "db", "roots", "scanner", "shellcmd"},
    "filters": {"core", "db", "shellcmd"},
    "organize": {"core", "duplicates", "shellcmd"},
    "parasites": {"core", "db", "scanner", "shellcmd"},
    "preview": {"db", "duplicates"},
    "projects": {"parasites"},
    "roots": {"core"},
    "scanner": {"core", "db", "roots", "shellcmd"},
    "similar": {"db"},
    "core": set(),
    "shellcmd": set(),
    "bench": set(),
}

# The interfaces and what starts them: the engine never depends on them.
INTERFACES = {"__main__", "entry", "cli", "web", "gateway"}

# The one accepted cycle: db stores the records scanner.models defines, and
# scanner.runner writes them through db. Moving the models to core would
# break it; not worth the churn while both packages change together.
ACCEPTED_CYCLES = {frozenset({"db", "scanner"})}


def _unit(module: str) -> str:
    """'core.sizes' -> 'core'; 'cli' -> 'cli'."""
    return module.split(".")[0]


def _imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for path in SRC.rglob("*.py"):
        source = _unit(".".join(path.relative_to(SRC).with_suffix("").parts))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.level == 0, f"{path}: relative import"
                if node.module == "treeaudit":
                    targets = [alias.name for alias in node.names]
                elif node.module.startswith("treeaudit."):
                    targets = [node.module.removeprefix("treeaudit.")]
            elif isinstance(node, ast.Import):
                targets = [
                    alias.name.removeprefix("treeaudit.")
                    for alias in node.names
                    if alias.name.startswith("treeaudit.")
                ]
            for target in targets:
                unit = _unit(target)
                if unit != source and ((SRC / unit).is_dir() or (SRC / f"{unit}.py").is_file()):
                    found[source].add(unit)
    return found


def test_every_package_is_listed() -> None:
    units = {p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    units |= {p.stem for p in SRC.glob("*.py") if p.stem != "__init__"}
    assert units == set(ALLOWED)


def test_only_the_allowed_dependencies() -> None:
    unexpected = {
        f"{source} -> {target}"
        for source, targets in _imports().items()
        for target in targets - ALLOWED[source]
    }
    assert not unexpected, f"add to ALLOWED on purpose, or remove: {sorted(unexpected)}"


def test_the_engine_never_imports_an_interface() -> None:
    for source, targets in ALLOWED.items():
        if source not in INTERFACES:
            assert not targets & INTERFACES, source


def test_core_depends_on_nothing_else() -> None:
    assert ALLOWED["core"] == set()


def test_no_cycle_but_the_accepted_one() -> None:
    graph = _imports()
    cycles = {
        frozenset({a, b}) for a, targets in graph.items() for b in targets if a in graph.get(b, ())
    }
    assert cycles == ACCEPTED_CYCLES


def test_the_checker_sees_the_known_dependencies() -> None:
    """Sanity check of the reader itself: the graph is not empty and sees the
    known edges, so an empty result cannot pass the tests above."""
    graph = _imports()
    assert "web" in graph["cli"] and "core" in graph["roots"] and "entry" in graph["__main__"]
